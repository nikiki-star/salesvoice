"""端到端验证：后端 API 全链路。

用法：先启动 server，再跑本脚本。
    PYTHONPATH=src .venv/bin/python -m salesvoice.server &
    .venv/bin/python tests/e2e_test.py
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8777"
ROOT = Path(__file__).resolve().parents[1]


def call(method: str, path: str, payload: dict | None = None, timeout: int = 300):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:300]}"}


def step(name, fn):
    print(f"\n{'='*72}\n▶ {name}\n{'='*72}")
    try:
        out = fn()
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 失败: {exc}")
        return None


# 1. 健康检查
h = step("健康检查 /api/health", lambda: call("GET", "/api/health"))
print(json.dumps(h, ensure_ascii=False, indent=2) if h else "")
assert h and h.get("ok"), "后端未就绪"
assert h.get("has_key"), "缺少 LLM 凭据"

# 2. 录入会面（转录文本 → 情报抽取 → 入库）
transcript = (ROOT / "tests" / "sample_transcript.txt").read_text(encoding="utf-8")
ing = step("录入会面 /api/ingest（含 LLM 抽取，约 30-90 秒）", lambda: call("POST", "/api/ingest", {
    "client_name": "王总",
    "company": "鑫达五金",
    "title": "董事长",
    "industry": "五金制造",
    "met_on": "2026-09-16",
    "location": "客户工厂会客室",
    "attendees": "王总、销售小李",
    "transcript": transcript,
}))
assert ing and ing.get("ok"), f"录入失败: {ing}"
print(f"✓ 客户 {ing['client_id']} · 会面 {ing['meeting_id']}")
print(f"✓ 写入 {ing['tags_written']} 条标签，雷区 {ing['landmines']} 条，"
      f"证据回验通过 {ing['evidence_verified']} 条")

# 3. 读取画像
prof = step("读取客户画像 /api/clients/<id>", lambda: call("GET", f"/api/clients/{ing['client_id']}"))
assert prof and prof.get("ok")
c, s = prof["client"], prof["stats"]
print(f"✓ {c['name']} · {c['company']} · {c['title']}")
print(f"✓ 标签 {s['tags']} 条 | 雷区 {s['landmines']} | 高危 {s['high_severity']} "
      f"| 证据回验率 {s['verified_rate']*100:.0f}%")
for cid, meta in prof["categories"].items():
    if meta["items"]:
        print(f"\n  【{meta['label']}】")
        for t in meta["items"]:
            sev = f" [{t['severity']}]" if t["severity"] else ""
            print(f"    · {t['key']}: {t['value'][:52]}{sev}")

# 4. AI 建议
adv = step("生成会前简报 /advice?type=brief（LLM，约 30-60 秒）",
           lambda: call("GET", f"/api/clients/{ing['client_id']}/advice?type=brief"))
if adv and adv.get("ok"):
    print(adv["markdown"][:1400])
    print(f"\n[引用标签] {', '.join(adv.get('based_on', []))}")

# 5. 宴请建议
ban = step("生成宴请建议 /advice?type=banquet",
           lambda: call("GET", f"/api/clients/{ing['client_id']}/advice?type=banquet"))
if ban and ban.get("ok"):
    print(ban["markdown"][:1200])

# 6. 雷区预检
chk = step("雷区预检（场景：带两瓶茅台 + 聊他儿子接班）", lambda: call("POST", f"/api/clients/{ing['client_id']}/check", {
    "scenario": "这次见面带两瓶茅台过去，安排在一个粤菜馆，饭桌上聊聊他儿子接班的事",
}))
if chk:
    print(f"风险等级: {chk.get('risk')}")
    print(f"结论: {chk.get('verdict')}")
    for hh in chk.get("hits", []):
        print(f"  ⚠ 命中【{hh.get('key')}】{hh.get('reason','')}")
        print(f"     → {hh.get('advice','')}")

# 7. 客户列表与检索
cl = step("客户列表 /api/clients", lambda: call("GET", "/api/clients"))
if cl:
    for x in cl["clients"]:
        print(f"  · {x['name']} | {x['meeting_count']} 次会面 | {x['tag_count']} 标签 | "
              f"雷区 {x['landmine_count']}")

sr = step("全文检索 /api/search?q=酒", lambda: call("GET", "/api/search?q=%E9%85%92"))
if sr:
    for r in sr["results"][:6]:
        print(f"  · [{r['client_name']}] {r['key']}: {r['value'][:44]}")

print("\n" + "=" * 72)
print("端到端验证完成")
print("=" * 72)
