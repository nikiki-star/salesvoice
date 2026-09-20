"""命令行入口。

    python -m salesvoice.cli serve                     启动看板
    python -m salesvoice.cli setup-models              下载本机 ASR 模型
    python -m salesvoice.cli ingest --audio 录音.m4a --client 王总
    python -m salesvoice.cli ingest --text 对话.txt --client 王总
    python -m salesvoice.cli list                      客户列表
    python -m salesvoice.cli show 王总                 查看画像
    python -m salesvoice.cli advice 王总 --type banquet
    python -m salesvoice.cli check 王总 "带两瓶茅台过去"
    python -m salesvoice.cli search 白酒
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import config
from .store import Store


def _resolve_client(store: Store, key: str) -> dict:
    """按 id 或名字找客户。"""
    for c in store.list_clients():
        if c["id"] == key or c["name"] == key:
            return c
    raise SystemExit(f"未找到客户：{key}（用 list 查看已有客户）")


def cmd_serve(args) -> int:
    from .server import serve

    serve(args.port)
    return 0


def cmd_setup_models(args) -> int:
    from .setup_models import main

    return main()


def cmd_notion_sources(args) -> int:
    from .notion_sync import list_sources

    try:
        list_sources()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_notion_init(args) -> int:
    """在指定 Notion 页面下建一个结构匹配 salesvoice 的「客户会面录音」数据库。"""
    from .notion_client import NotionClient

    try:
        client = NotionClient()
        me = client.check()
        print(f"[notion] 集成：{me['name']}（{me['id']}）")
        db_id, ds_id, props = client.create_meeting_database(args.parent, args.title)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {exc}", file=sys.stderr)
        return 1

    print(f"✓ 已创建数据库：{args.title}")
    print(f"  database_id    : {db_id}")
    print(f"  data_source_id : {ds_id}")
    print(f"  属性：{'、'.join(props)}")
    print("\n接下来把它设成投放区（用 data_source_id 查询最稳）：")
    print(f"  export SALESVOICE_NOTION_SOURCE={ds_id or db_id}")
    print("  或跨会话固定：把它写进 ~/.hermes/.env")
    print("\n别忘了在该数据库页面里：… → 连接 → 选你的集成"
          "（新建的数据源通常已随父页面共享，但换页面时要重新连接）")
    return 0


def cmd_notion_sync(args) -> int:
    from .notion_sync import FixtureSource, NotionApiSource, sync_notion

    store = Store()
    if args.fixture:
        print(f"[notion] 离线 fixture 模式：{args.fixture}（不联网、不需要 token）")
        source = FixtureSource(args.fixture)
    else:
        if not config.has_notion_key():
            print(
                "未找到 Notion 集成 token。四步配好：\n"
                "  1) 打开 notion.so/my-integrations → 新建内部集成，复制 token（ntn_/secret_ 开头）\n"
                "  2) 写进 ~/.hermes/.env：NOTION_API_KEY=ntn_xxxx（别贴在聊天里）\n"
                "  3) 在 Notion 里打开「录音/会面记录」数据库 → … → 连接 → 选该集成\n"
                "  4) export SALESVOICE_NOTION_SOURCE=<该数据库或页面的 URL/ID>\n"
                "  然后：salesvoice notion-sync --dry-run 先看会做什么\n"
                "  没有现成数据库：salesvoice notion-init --parent <页面URL> 自动建好结构",
                file=sys.stderr)
            return 2
        if not config.NOTION_SOURCE:
            print("未设置投放区 SALESVOICE_NOTION_SOURCE。先用 "
                  "`salesvoice notion-sources` 从集成可见的候选里挑一个。", file=sys.stderr)
            return 2
        try:
            source = NotionApiSource()
        except Exception as exc:  # noqa: BLE001
            print(f"✗ {exc}", file=sys.stderr)
            return 1
        print(f"[notion] 投放区：{source.describe()}")

    mode = "预演（不写库、不反写）" if args.dry_run else "正式同步"
    wb = "跟随配置" if args.writeback is None else ("开" if args.writeback else "关")
    print(f"[notion] {mode} · 反写：{wb} · 上限：{args.limit or '不限'}\n")

    report = sync_notion(store, source=source, dry_run=args.dry_run, limit=args.limit,
                         page_id=args.page, writeback=args.writeback, force=args.force,
                         verbose=True)

    print(f"\n{'=' * 72}")
    print(f"扫描 {report['scanned']} 个页面 → 入库 {report['ingested']}，"
          f"跳过 {report['skipped']}，失败 {report['failed']}")
    print(f"{'=' * 72}")
    if args.json:
        import json as _json

        print(_json.dumps(report, ensure_ascii=False, indent=2))
    if not args.dry_run and report["ingested"]:
        print("\n看板：salesvoice serve 后打开 http://127.0.0.1:8777")
    return 0 if report["ok"] else 1


def cmd_ingest(args) -> int:
    from .extract import extract_tags, summarize_meeting
    from .transcribe import segments_to_marked_text, transcribe

    store = Store()
    transcript = ""

    if args.audio:
        print(f"[1/3] 本机转写音频：{args.audio}")
        print(f"      引擎 {args.engine}（音频不出设备）")
        out = transcribe(args.audio, engine=args.engine)
        transcript = segments_to_marked_text(out["segments"], args.speaker)
        print(f"      ✓ {len(out['segments'])} 段，时长 {out.get('duration', 0):.0f} 秒")
        dump = config.TRANSCRIPT_DIR / f"{Path(args.audio).stem}.txt"
        dump.write_text(transcript, encoding="utf-8")
        print(f"      ✓ 转录已存 {dump}")
    elif args.text:
        p = Path(args.text)
        transcript = p.read_text(encoding="utf-8") if p.is_file() else args.text
    else:
        print("请提供 --audio 或 --text", file=sys.stderr)
        return 2

    if not transcript.strip():
        print("转录为空，终止", file=sys.stderr)
        return 1

    print("[2/3] 抽取客户情报（LLM）…")
    met_on = args.date or datetime.now().strftime("%Y-%m-%d")
    cid = store.upsert_client(args.client, company=args.company or "",
                              title=args.title or "", industry=args.industry or "")
    summary = summarize_meeting(transcript, args.client)
    mid = store.add_meeting(cid, met_on, transcript=transcript,
                            summary=summary.get("summary", ""),
                            location=args.location or "",
                            audio_path=args.audio or "")
    tags = extract_tags(transcript, client_name=args.client, meeting_id=mid,
                        engine=args.extract_engine)
    n = store.add_tags(cid, tags, meeting_id=mid)

    print(f"[3/3] 已入库 {n} 条标签")
    for t in tags:
        mark = "✓" if t.evidence_verified else "✗"
        sev = f" [{t.severity}]" if t.severity else ""
        print(f"   {mark} [{t.category}] {t.key}: {t.value}{sev}")
    print(f"\n客户 ID: {cid}\n查看画像: python -m salesvoice.cli show {args.client}")
    return 0


def cmd_list(args) -> int:
    store = Store()
    clients = store.list_clients()
    if not clients:
        print("暂无客户。先用 ingest 录入一次会面。")
        return 0
    print(f"{'名称':<12}{'单位':<20}{'会面':>5}{'标签':>6}{'雷区':>6}  最近")
    print("-" * 72)
    for c in clients:
        unit = (c["company"] or "")[:18]
        print(f"{c['name']:<12}{unit:<20}{c['meeting_count']:>5}{c['tag_count']:>6}"
              f"{c['landmine_count']:>6}  {c['last_met'] or '-'}")
    return 0


def cmd_show(args) -> int:
    store = Store()
    c = _resolve_client(store, args.client)
    p = store.profile(c["id"])
    s = p["stats"]
    print(f"\n{p['client']['name']} · {p['client']['company']} {p['client']['title']}")
    print(f"标签 {s['tags']} | 雷区 {s['landmines']} | 高危 {s['high_severity']} | "
          f"证据回验率 {s['verified_rate']*100:.0f}%\n")
    for cid, meta in p["categories"].items():
        if not meta["items"]:
            continue
        print(f"【{meta['label']}】")
        for t in meta["items"]:
            sev = f" ({t['severity']})" if t["severity"] else ""
            mark = "" if t["evidence_ok"] else "  ⚠未回验"
            print(f"  · {t['key']}: {t['value']}{sev}{mark}")
            if t["trigger_when"]:
                print(f"      触发: {t['trigger_when']}")
            if args.evidence and t["evidence"]:
                print(f"      原话: 「{t['evidence']}」")
        print()
    if p["meetings"]:
        print("会面记录：")
        for m in p["meetings"]:
            print(f"  {m['met_on']} @ {m['location'] or '未记录'}")
    return 0


def cmd_advice(args) -> int:
    from .suggest import generate_advice

    store = Store()
    c = _resolve_client(store, args.client)
    out = generate_advice(store.profile(c["id"]), args.type)
    print(f"\n【{out['label']}】{c['name']}\n" + "=" * 60)
    print(out["markdown"])
    if out.get("based_on"):
        print("\n引用标签：" + " · ".join(out["based_on"]))
    return 0


def cmd_check(args) -> int:
    from .suggest import quick_landmine_check

    store = Store()
    c = _resolve_client(store, args.client)
    out = quick_landmine_check(store.profile(c["id"]), args.scenario)
    label = {"high": "🔴 高危", "medium": "🟠 有风险", "low": "🟡 轻微",
             "none": "🟢 未发现踩雷", "unknown": "⚪ 无法判断"}.get(out["risk"], out["risk"])
    print(f"\n{label} —— {out.get('verdict', '')}")
    for h in out.get("hits", []):
        print(f"\n  ⚠ 命中雷区：{h.get('key')}")
        print(f"    原因：{h.get('reason', '')}")
        if h.get("advice"):
            print(f"    建议：{h['advice']}")
    return 0 if out["risk"] in ("none", "low") else 1


def cmd_search(args) -> int:
    store = Store()
    results = store.search(args.query)
    if not results:
        print("无匹配。")
        return 0
    for r in results:
        print(f"[{r['client_name']}] {r['key']}: {r['value']}")
        if r["evidence"]:
            print(f"    「{r['evidence'][:70]}」")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="salesvoice", description="客户语音情报中台")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="启动网页看板")
    p.add_argument("port", nargs="?", type=int, default=8777)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("setup-models", help="下载本机语音识别模型（约 1 GB）")
    p.set_defaults(func=cmd_setup_models)

    p = sub.add_parser("notion-sync", help="从 Notion 拉取录音/文字稿并入库")
    p.add_argument("--dry-run", action="store_true", help="只扫描不写库（先看会做什么）")
    p.add_argument("--limit", type=int, help="最多处理多少个页面")
    p.add_argument("--page", help="只处理指定 Notion 页面 ID")
    p.add_argument("--force", action="store_true", help="忽略内容哈希，强制重新抽取")
    p.add_argument("--writeback", dest="writeback", action="store_true", default=None,
                   help="勾「已同步」并把回执写回 Notion")
    p.add_argument("--no-writeback", dest="writeback", action="store_false",
                   help="只读，不改动 Notion（默认）")
    p.add_argument("--fixture", help="离线模式：读本地 fixture 目录，不联网")
    p.add_argument("--json", action="store_true", help="输出完整报告的 JSON")
    p.set_defaults(func=cmd_notion_sync)

    p = sub.add_parser("notion-sources", help="列出集成可见的 Notion 数据库/页面")
    p.set_defaults(func=cmd_notion_sources)

    p = sub.add_parser("notion-init", help="在指定页面下创建结构匹配的录音数据库")
    p.add_argument("--parent", required=True, help="父页面 ID 或 URL")
    p.add_argument("--title", default="客户会面录音", help="数据库标题")
    p.set_defaults(func=cmd_notion_init)

    p = sub.add_parser("ingest", help="录入一次会面")
    p.add_argument("--audio", help="音频文件路径（本机转写）")
    p.add_argument("--text", help="转录文本或文本文件路径")
    p.add_argument("--client", required=True, help="客户名称")
    p.add_argument("--date", help="会面日期 YYYY-MM-DD")
    p.add_argument("--company", help="客户公司")
    p.add_argument("--title", help="客户职位")
    p.add_argument("--industry", help="所属行业")
    p.add_argument("--location", help="会面地点")
    p.add_argument("--speaker", default="客户", help="音频转写时的说话人标签")
    p.add_argument("--engine", default="sensevoice", help="转写引擎 sensevoice/cloud")
    p.add_argument("--extract-engine", default="default",
                   choices=["default", "langextract"],
                   help="情报抽取引擎：default=自研（中文子串回验）；"
                        "langextract=google/langextract（char_interval 对齐）")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("list", help="客户列表")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="查看客户画像")
    p.add_argument("client")
    p.add_argument("--evidence", action="store_true", help="同时显示原话证据")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("advice", help="生成行动建议")
    p.add_argument("client")
    p.add_argument("--type", default="brief",
                   choices=["brief", "banquet", "followup", "gift"])
    p.set_defaults(func=cmd_advice)

    p = sub.add_parser("check", help="雷区预检")
    p.add_argument("client")
    p.add_argument("scenario", help="你计划做的事")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("search", help="跨客户检索情报")
    p.add_argument("query")
    p.set_defaults(func=cmd_search)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
