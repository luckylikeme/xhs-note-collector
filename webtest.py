# -*- coding: utf-8 -*-
"""Web 服务端到端自测 + 前端截图。

用法（服务需已启动）：

    export XHS_TEST_IDENT=<你的 user_id 或登录 key>   # 可选，不设则跳过取数相关检查
    python webtest.py

可选：XHS_BASE 指定服务地址，默认 http://127.0.0.1:8320
"""
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("XHS_BASE", "http://127.0.0.1:8320").rstrip("/")
IDENT = os.environ.get("XHS_TEST_IDENT", "").strip()
OUT = Path("out")
OUT.mkdir(parents=True, exist_ok=True)


def get(path, timeout=180):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, r.headers.get("content-type", ""), r.read()


print("--- GET / ---")
s, ct, b = get("/")
print(f"HTTP {s}  {ct}  {len(b)} bytes  含标题={('小红书笔记数据'.encode() in b)}")

print("--- GET /api/health ---")
s, ct, b = get("/api/health")
print(s, ct, b.decode())

if IDENT:
    print("--- GET /api/notes.csv ---")
    s, ct, b = get(f"/api/notes.csv?key={IDENT}&user_id={IDENT}")
    print(f"HTTP {s}  {ct}  {len(b)} bytes")
    for line in b.decode("utf-8-sig").splitlines()[:2]:
        print("  " + line)

print("--- 错误分支 ---")
for path in ("/api/notes?user_id=__not_exist__", "/api/login/status?key=nosuchkey"):
    try:
        get(path, timeout=20)
    except urllib.error.HTTPError as e:
        print(f"  {path} -> HTTP {e.code}  {e.read().decode()[:90]}")

# ---------------- 前端截图 ----------------
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("未安装 playwright，跳过截图")
    sys.exit(0)

print("--- 前端截图 ---")
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1280, "height": 960})
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    page.click("#btn-start")
    for _ in range(40):
        if page.evaluate("() => !!document.querySelector('#qr-frame img')"):
            break
        page.wait_for_timeout(1000)
    page.wait_for_timeout(1500)
    target = OUT / "ui.png"
    page.screenshot(path=str(target), full_page=True)
    print(f"saved {target}")
    browser.close()
