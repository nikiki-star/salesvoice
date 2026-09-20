"""Notion 同步层测试（完全离线：不联网、不需要 token、不调用 LLM）。

覆盖三件事：
  1. **解析**：Notion 的页面属性/块 → 入库草稿。含中文属性名、日期、多选、文件属性、
     子页面文字稿、以及「回执块必须排除在转录之外」（否则第二次同步会把上次的摘要
     当成客户原话喂给模型，形成自我喂养）。
  2. **幂等**：按内容哈希去重 —— 内容没变的页面再同步应当跳过；--force 才重跑。
  3. **音频分支**：页面里没有文字稿但有音频时，走本机转写（测试里注入假转写器）。

真正的 Notion HTTP 行为（401/404/分页）在 tests/test_server_smoke.py 与人工运行时验证，
这里用 FixtureSource 隔离，保证 CI 上零外部条件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from salesvoice.notion_client import extract_id, rich_text_paragraph
from salesvoice.notion_sync import (WRITEBACK_MARKER, AudioRef, FixtureSource, build_draft,
                                    content_hash, is_writeback_block, sync_notion,
                                    transcript_from_blocks)
from salesvoice.schema import Tag
from salesvoice.store import Store

# 一段够长的中文转录（>= MIN_TRANSCRIPT_CHARS）
TRANSCRIPT = (
    "我痛风好几年了，医生说酒一口都不能碰。谁要是硬劝我，我直接就翻脸。\n"
    "吃饭我不吃辣，清淡点就行，最好是粤菜。我这人没什么别的爱好，"
    "就爱这口普洱，再就是钓钓鱼。"
)


# ---------------------------------------------------------------- 构造 Notion 数据


def _page(page_id: str = "1f2a3b4c5d6e7f8091a2b3c4d5e6f708", **props) -> dict:
    properties = {
        "客户名称": {"type": "title", "title": [
            {"type": "text", "plain_text": props.pop("client", "王总 2026-09-20 会面")}]},
        "公司": {"type": "rich_text", "rich_text": [
            {"type": "text", "plain_text": props.pop("company", "鑫达五金")}]},
        "日期": {"type": "date", "date": {"start": props.pop("date", "2026-09-16")}},
        "参与人": {"type": "rich_text", "rich_text": [
            {"type": "text", "plain_text": props.pop("attendees", "王总、销售小李")}]},
        "地点": {"type": "rich_text", "rich_text": [
            {"type": "text", "plain_text": props.pop("location", "客户工厂会客室")}]},
        "转录": {"type": "rich_text", "rich_text": [
            {"type": "text", "plain_text": props.pop("transcript", "")}]},
        "已同步": {"type": "checkbox", "checkbox": False},
    }
    properties.update(props.pop("extra_props", {}))
    return {"object": "page", "id": page_id, "created_time": "2026-09-16T02:00:00.000Z",
            "properties": properties}


def _para(text: str, block_id: str = "b1") -> dict:
    return {"object": "block", "id": block_id, "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "plain_text": text}]}}


def _audio_block(url: str, name: str = "录音.m4a", block_id: str = "b9") -> dict:
    return {"object": "block", "id": block_id, "type": "audio",
            "audio": {"type": "file", "name": name, "file": {"url": url}}}


def _writeback_block(block_id: str = "wb1") -> dict:
    return {"object": "block", "id": block_id, "type": "callout",
            "callout": {"rich_text": [{"type": "text",
                                       "plain_text": f"{WRITEBACK_MARKER} 已同步到 SalesVoice · 12 条标签"}]}}


# ---------------------------------------------------------------- 假依赖


def _fake_tags(client_name: str, meeting_id: str) -> list[Tag]:
    return [
        Tag(category="landmine", key="劝酒", value="硬劝酒会翻脸", evidence="谁要是硬劝我。我直接就翻脸",
            severity="high", trigger="饭局劝酒", meeting_id=meeting_id,
            evidence_verified=True, evidence_mode="exact"),
        Tag(category="preference", key="菜系", value="偏好粤菜、清淡", evidence="吃饭我不吃辣。清淡点就行",
            meeting_id=meeting_id, evidence_verified=True, evidence_mode="fuzzy"),
    ]


def _fake_ingest(store, payload):
    """模拟 server.ingest_meeting 的落库行为（不调用 LLM）。"""
    cid = store.upsert_client(name=payload["client_name"], company=payload.get("company", ""))
    mid = store.add_meeting(cid, payload.get("met_on") or "2026-01-01",
                           transcript=payload.get("transcript", ""),
                           location=payload.get("location", ""),
                           attendees=payload.get("attendees", ""),
                           audio_path=payload.get("audio_path", ""))
    tags = _fake_tags(payload["client_name"], mid)
    n = store.add_tags(cid, tags, meeting_id=mid)
    return {"ok": True, "client_id": cid, "meeting_id": mid, "tags_written": n,
            "landmines": sum(1 for t in tags if t.category == "landmine"),
            "evidence_verified": sum(1 for t in tags if t.evidence_verified),
            "tags": [t.to_dict() for t in tags]}


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


@pytest.fixture()
def fixture_dir(tmp_path) -> Path:
    """一个「录音已录制、转录已在页面里」的投放区。"""
    d = tmp_path / "fixture"
    d.mkdir()
    page = _page()
    (d / "a.page.json").write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    blocks = [_para(TRANSCRIPT, "b1"), _writeback_block()]
    (d / "1f2a3b4c.blocks.json").write_text(json.dumps(blocks, ensure_ascii=False),
                                           encoding="utf-8")
    return d


# ---------------------------------------------------------------- ID 与工具


def test_extract_id_accepts_url_uuid_and_bare():
    bare = "1f2a3b4c5d6e7f8091a2b3c4d5e6f708"
    dashed = "1f2a3b4c-5d6e-7f80-91a2-b3c4d5e6f708"
    assert extract_id(f"https://www.notion.so/ws/My-DB-{bare}?v=abc") == dashed
    assert extract_id(f"https://www.notion.so/{bare}") == dashed
    assert extract_id(dashed) == dashed
    assert extract_id(bare) == dashed


def test_rich_text_paragraph_chunks_by_line():
    items = rich_text_paragraph("第一行\n第二行", limit=6)
    assert len(items) == 2
    assert items[0]["text"]["content"] == "第一行"
    assert items[1]["text"]["content"] == "第二行"
    assert rich_text_paragraph("")[0]["text"]["content"] == ""


# ---------------------------------------------------------------- 解析


def test_build_draft_from_properties():
    draft = build_draft(_page(transcript=TRANSCRIPT))
    assert draft.client_name == "王总"                     # 日期与「会面」后缀被剥掉
    assert draft.company == "鑫达五金"
    assert draft.met_on == "2026-09-16"
    assert draft.attendees == "王总、销售小李"
    assert draft.location == "客户工厂会客室"
    assert draft.transcript.startswith("我痛风好几年了")
    assert draft.synced_prop == "已同步"
    assert draft.has_content


@pytest.mark.parametrize("title,want", [
    ("周工 2026-09-19 现场拜访", "周工"),
    ("周工 2026-08-28 微信语音留言", "周工"),
    ("王总 2026-09-16 会面", "王总"),
    ("李总 2026-09-18 现场拜访记录", "李总"),
    ("2026-08-28 鑫达五金 电话沟通", "鑫达五金"),
    ("陈总 09-19 来电", "陈总"),
    ("张三", "张三"),
    ("远达机械 技术交流 2026-07-02", "远达机械"),
])
def test_client_name_from_varied_titles(title, want):
    """同一客户的不同会面标题必须收敛到同一个名字，否则跨会面累积会断。"""
    assert build_draft(_page(client=title)).client_name == want


def test_transcript_from_blocks_skips_writeback_block():
    blocks = [_para(TRANSCRIPT), _writeback_block()]
    text = transcript_from_blocks(blocks)
    assert "痛风" in text
    assert WRITEBACK_MARKER not in text
    assert is_writeback_block(_writeback_block())
    assert not is_writeback_block(_para("普通段落"))


def test_build_draft_prefers_property_then_body_then_child_page():
    # 正文够长 → 用正文
    d1 = build_draft(_page(), [_para(TRANSCRIPT)])
    assert "痛风" in d1.transcript
    # 正文为空 → 用子页面文字稿
    d2 = build_draft(_page(), [_para("短")], child_texts=[TRANSCRIPT])
    assert "痛风" in d2.transcript
    # 属性里已有文字稿 → 优先属性
    d3 = build_draft(_page(transcript="属性里的文字稿" * 6), [_para("正文里的另一份" * 6)])
    assert d3.transcript.startswith("属性里的文字稿")


def test_build_draft_detects_audio_everywhere():
    page = _page(page_id="aaaa1111222233334444555566667777")
    page["properties"]["录音"] = {"type": "files", "files": [
        {"name": "会面.m4a", "file": {"url": "https://files.notion.so/x/会面.m4a?t=1"}}]}
    blocks = [_audio_block("https://files.notion.so/y/rec.mp3", "rec.mp3"),
              {"object": "block", "id": "b7", "type": "file",
               "file": {"type": "file", "name": "方案.pdf",
                        "file": {"url": "https://files.notion.so/z/方案.pdf"}}}]
    draft = build_draft(page, blocks)
    urls = [a.url for a in draft.audios]
    assert any("会面.m4a" in u for u in urls)      # files 属性里的音频
    assert any(u.endswith("rec.mp3") for u in urls)  # audio 块
    assert not any("方案.pdf" in u for u in urls)     # 非音频被排除


def test_content_hash_reacts_to_change():
    a = build_draft(_page(transcript=TRANSCRIPT), [])
    b = build_draft(_page(transcript=TRANSCRIPT + "补充一句。"), [])
    c = build_draft(_page(transcript=TRANSCRIPT), [_audio_block("https://x/a.m4a", "a.m4a")])
    assert content_hash(a) != content_hash(b)
    assert content_hash(a) != content_hash(c)


# ---------------------------------------------------------------- 同步主流程


def test_sync_ingests_then_skips_when_unchanged(store, fixture_dir):
    src = FixtureSource(fixture_dir)

    r1 = sync_notion(store, source=src, extract_fn=None, ingest_fn=_fake_ingest, verbose=False)
    assert r1["ingested"] == 1 and r1["failed"] == 0
    client = store.list_clients()[0]
    assert client["name"] == "王总" and client["tag_count"] == 2
    assert client["landmine_count"] == 1

    # 第二次：内容没变 → 跳过，不再重复入库（不重复烧 LLM，也不重复写标签）
    r2 = sync_notion(store, source=src, ingest_fn=_fake_ingest, verbose=False)
    assert r2["skipped"] == 1 and r2["ingested"] == 0
    assert len(store.list_clients()) == 1
    assert store.list_clients()[0]["meeting_count"] == 1

    # --force：强制重跑
    r3 = sync_notion(store, source=src, ingest_fn=_fake_ingest, force=True, verbose=False)
    assert r3["ingested"] == 1
    assert store.list_clients()[0]["meeting_count"] == 2


def test_sync_records_ledger(store, fixture_dir):
    sync_notion(store, source=FixtureSource(fixture_dir), ingest_fn=_fake_ingest, verbose=False)
    rows = store.notion_sync_list()
    assert len(rows) == 1
    assert rows[0]["status"] == "ingested"
    assert rows[0]["content_hash"]
    assert rows[0]["client_id"].startswith("cli_")


def test_sync_writeback_uses_marker_and_is_off_by_default(store, fixture_dir):
    src = FixtureSource(fixture_dir)
    sync_notion(store, source=src, ingest_fn=_fake_ingest, verbose=False)
    assert src.writebacks == []                     # 默认只读

    sync_notion(store, source=src, ingest_fn=_fake_ingest, writeback=True,
                force=True, verbose=False)
    assert len(src.writebacks) == 1
    assert WRITEBACK_MARKER in src.writebacks[0]["text"]
    assert "客户「王总」" in src.writebacks[0]["text"]


def test_sync_audio_branch_transcribes_locally(store, tmp_path):
    d = tmp_path / "audio_fixture"
    d.mkdir()
    # 页面只有音频、没有文字稿
    (d / "b.page.json").write_text(json.dumps(_page(client="李总", transcript=""), ensure_ascii=False),
                                   encoding="utf-8")
    (d / "1f2a3b4c.blocks.json").write_text(
        json.dumps([_audio_block("file:会面录音.m4a", "会面录音.m4a")], ensure_ascii=False),
        encoding="utf-8")
    (d / "会面录音.m4a").write_bytes(b"fake-audio-bytes")

    calls: list[str] = []

    def fake_transcribe(path: str) -> str:
        calls.append(path)
        return TRANSCRIPT

    r = sync_notion(store, source=FixtureSource(d), ingest_fn=_fake_ingest,
                    transcribe_fn=fake_transcribe, verbose=False)
    assert r["ingested"] == 1
    assert calls and calls[0].endswith(".m4a")
    assert r["results"][0]["audio"].endswith(".m4a")
    # 音频来源被标注进转录，便于回看这条情报是从哪段录音来的
    assert store.get_meetings(store.list_clients()[0]["id"])[0]["transcript"].startswith("[转录自音频")


def test_sync_skips_pages_without_content(store, tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    (d / "c.page.json").write_text(json.dumps(_page(client="空页面", transcript=""), ensure_ascii=False),
                                   encoding="utf-8")
    (d / "1f2a3b4c.blocks.json").write_text("[]", encoding="utf-8")
    r = sync_notion(store, source=FixtureSource(d), ingest_fn=_fake_ingest, verbose=False)
    assert r["skipped"] == 1 and r["ingested"] == 0
    assert store.list_clients() == []
    assert "既没有文字稿" in r["results"][0]["detail"]


def test_sync_dry_run_writes_nothing(store, fixture_dir):
    r = sync_notion(store, source=FixtureSource(fixture_dir), ingest_fn=_fake_ingest,
                    dry_run=True, verbose=False)
    assert r["results"][0]["status"] == "would-ingest"
    assert store.list_clients() == []
    assert store.notion_sync_list() == []


def test_sync_survives_one_bad_page(store, fixture_dir):
    """单页失败不能中断整批：坏页面记 failed，好页面照常入库。"""
    bad_id = "ffff1111222233334444555566667777"
    (fixture_dir / "d.page.json").write_text(json.dumps(_page(
        page_id=bad_id, client="坏页面"), ensure_ascii=False), encoding="utf-8")
    (fixture_dir / "ffff1111.blocks.json").write_text(
        json.dumps([_para(TRANSCRIPT, "b2")], ensure_ascii=False), encoding="utf-8")

    def flaky_ingest(st, payload):
        if payload["client_name"] == "坏页面":
            raise RuntimeError("模拟抽取失败")
        return _fake_ingest(st, payload)

    r = sync_notion(store, source=FixtureSource(fixture_dir), ingest_fn=flaky_ingest, verbose=False)
    assert r["ingested"] == 1 and r["failed"] == 1
    assert r["ok"] is False
    statuses = {x["status"] for x in store.notion_sync_list()}
    assert statuses == {"ingested", "failed"}


def test_fixture_source_missing_dir():
    with pytest.raises(RuntimeError):
        FixtureSource("/no/such/dir")
