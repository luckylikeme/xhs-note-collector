# -*- coding: utf-8 -*-
"""API 冒烟测试：验证 /api/notes 是否支持 key 和 user_id 两种入口。

用法（服务需已启动，且该账号已扫码登录过）：

    # macOS / Linux
    export XHS_TEST_IDENT=<你的 user_id 或登录 key>
    python test_api.py

    # Windows
    set XHS_TEST_IDENT=<你的 user_id 或登录 key>
    python test_api.py

可选：XHS_BASE 指定服务地址，默认 http://127.0.0.1:8320
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("XHS_BASE", "http://127.0.0.1:8320").rstrip("/")
IDENT = os.environ.get("XHS_TEST_IDENT", "").strip()

if not IDENT:
    print("请先设置环境变量 XHS_TEST_IDENT（已登录的 user_id 或 登录 key）")
    sys.exit(2)


def get(path, timeout=300):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def brief(d):
    return (f"count={d.get('count')} user_id={d.get('user_id')!r} "
            f"key={d.get('key')!r} source={d.get('source')} "
            f"elapsed={d.get('elapsed_sec')}s")


print(f"BASE   = {BASE}")
print(f"IDENT  = {IDENT}")
print("注意：两种入口里，只有和这个 IDENT 类型匹配的那一个会返回 200，另一个应当 404。\n")

print("=== 1) 已登录账号列表 ===")
s, d = get("/api/accounts")
print(s, json.dumps(d, ensure_ascii=False))

print("\n=== 2) 用 key 取数 ===")
s, d = get(f"/api/notes?key={IDENT}")
print(s, brief(d))

print("\n=== 3) 用 user_id 取数 ===")
s, d = get(f"/api/notes?user_id={IDENT}")
print(s, brief(d))

print("\n=== 4) 错误分支（都应返回 4xx） ===")
for p in ("/api/notes",
          "/api/notes?key=__not_exist__",
          "/api/notes?user_id=__not_exist__",
          f"/api/notes?key={IDENT}&start=2026/04/01"):
    s, d = get(p, timeout=30)
    detail = str(d.get("detail", ""))[:70]
    print(f"  {p:<48} -> HTTP {s}  {detail}")

print("\n=== 5) CSV ===")
try:
    with urllib.request.urlopen(f"{BASE}/api/notes.csv?key={IDENT}", timeout=300) as r:
        body = r.read()
    print(f"HTTP {r.status}  {r.headers.get('content-type')}  {len(body)} bytes")
    print("  " + body.decode("utf-8-sig").splitlines()[0])
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}（该 IDENT 不是 key，属正常）")
