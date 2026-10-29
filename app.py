#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小红书笔记数据 Web 服务
=======================

三个 API
--------
POST /api/login/start      前端传一个随机 key，服务端起无头浏览器，把登录二维码
                           以 data URI 返回；该 key 30 分钟内有效。
GET  /api/login/status     前端轮询，返回 pending / success / expired；
                           成功时带 user_id（小红书号），并持续返回刷新后的二维码。
GET  /api/notes            传 user_id（小红书号）或 key，返回该账号全量笔记基础数据（JSON）。

设计要点
--------
1. **全程无头**，不渲染任何浏览器界面（headless=True）。
2. **二维码会过期**：小红书二维码本身约 3 分钟失效，而会话语义是 30 分钟，
   所以后台每 45 秒重新取一次二维码，前端每次轮询 status 都会拿到最新的码。
3. **登录成功后立刻关浏览器**：登录态写入 profile 目录（Playwright 持久化），
   取数据时再从该 profile 起一个浏览器。省内存，且服务重启不丢登录态。
4. **两条路合并取数**：官方「导出数据」给全指标（曝光/点击率/人均时长/弹幕），
   数据接口给 note_id，用「首次发布时间」做联合主键。

启动
----
    uvicorn app:app --host 0.0.0.0 --port 8000
或：
    python app.py
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import random
import re
import string
import sys
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

import xhs_creator_collect as xhs

# ---------------------------------------------------------------- 配置

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("XHS_DATA_DIR", BASE_DIR / "data"))
PROFILE_DIR = DATA_DIR / "profiles"
EXPORT_DIR = DATA_DIR / "exports"
USERS_FILE = DATA_DIR / "users.json"
WEB_DIR = BASE_DIR / "web"

QR_SESSION_TTL = int(os.environ.get("XHS_QR_TTL", 30 * 60))     # 登录会话 30 分钟
QR_REFRESH_INTERVAL = 45                                        # 二维码刷新间隔
POLL_INTERVAL = 2.5                                             # 后台轮询间隔
MAX_SESSIONS = int(os.environ.get("XHS_MAX_SESSIONS", 4))        # 同时最多几个登录会话
MAX_PAGES = int(os.environ.get("XHS_MAX_PAGES", 60))
HEADLESS = True   # 需求：绝不渲染浏览器界面


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- 数据结构

def ttl_text() -> str:
    if QR_SESSION_TTL >= 120:
        return f"{QR_SESSION_TTL // 60} 分钟"
    return f"{QR_SESSION_TTL} 秒"


@dataclass
class QrSession:
    key: str
    profile: Path
    created_at: float
    expires_at: float
    context: Any = None
    page: Any = None
    qr: str = ""                 # data:image/png;base64,...
    qr_at: float = 0.0
    status: str = "pending"      # pending / success / expired / error
    user_id: str = ""
    message: str = ""
    task: asyncio.Task | None = None


# ---------------------------------------------------------------- 历史登录账号
#
# 存储结构 version 2：以 **key** 为主键。
# 原因：key 在"发起登录"那一刻就存在，而 user_id（小红书号）要等登录成功才拿到，
# 还可能一时读不到。所以让 key 能独立工作，user_id 只是它的一个可选索引。
#
# {
#   "version": 2,
#   "accounts": {
#     "<key>": {"key": "...", "user_id": "...", "profile": "...", "logged_in_at": "..."}
#   }
# }

def migrate_accounts(raw: dict) -> dict[str, dict]:
    """把账号表规范成 v2（key -> record）；兼容旧格式 user_id -> record。

    纯函数，不碰文件系统，方便单测。
    """
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") == 2 and isinstance(raw.get("accounts"), dict):
        return raw["accounts"]
    out: dict[str, dict] = {}
    for uid, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        key = rec.get("key") or f"user-{uid}"
        new = dict(rec)
        new["key"] = key
        new["user_id"] = new.get("user_id") or uid
        out[key] = new
    return out


def load_accounts() -> dict[str, dict]:
    try:
        raw = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return migrate_accounts(raw)


def save_accounts(accounts: dict[str, dict]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "accounts": accounts}
    USERS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 浏览器助手

def ns(**kw) -> SimpleNamespace:
    """给 xhs_creator_collect 里的函数造一个 args 替身。"""
    base = dict(profile="", headful=False, no_sandbox=False, dashboard_url=None)
    base.update(kw)
    return SimpleNamespace(**base)


async def qr_data_uri(page) -> str:
    """从登录页取二维码原图的 data URI（内部会自动从短信模式切到扫码）。"""
    try:
        sms = page.get_by_text("短信登录", exact=False).first
        if await sms.count() > 0 and await sms.is_visible():
            log("登录页停在短信模式，切到扫码")
            await xhs._click_qr_toggle(page)
    except Exception:
        pass
    qr = await xhs._wait_qr(page, 15000)
    if qr is None:
        await xhs._click_qr_toggle(page)
        qr = await xhs._wait_qr(page, 8000)
    return qr["uri"] if qr else ""


# 这两个接口用页面自己的 cookie 就能调，不需要 x-s 签名，是拿「小红书号」最稳的来源。
IDENTITY_ENDPOINTS = [
    ("/api/galaxy/user/info", "redId", ("userId", "userName")),
    ("/api/galaxy/creator/home/personal_info", "red_num", ("name",)),
]


async def read_identity(page, wait_seconds: float = 10.0) -> dict:
    """读本账号身份：{red_id, internal_id, nickname}。

    顺序：页面内接口 → DOM 文本 → 空。
    之前只读一次 DOM，扫码后 SPA 还没渲染出「小红书账号: xxx」就会误判失败。
    """
    out = {"red_id": "", "internal_id": "", "nickname": ""}
    if "creator.xiaohongshu.com" not in page.url:
        try:
            await page.goto(xhs.HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)
        except Exception:
            pass

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        for ep, red_key, extra_keys in IDENTITY_ENDPOINTS:
            try:
                data = await page.evaluate(
                    """async (u) => {
                        try {
                            const r = await fetch(u, {credentials: 'include'});
                            if (!r.ok) return null;
                            return await r.json();
                        } catch (e) { return null; }
                    }""", ep)
            except Exception:
                continue
            d = (data or {}).get("data") if isinstance(data, dict) else None
            if not isinstance(d, dict):
                continue
            rid = d.get(red_key)
            if rid:
                out["red_id"] = str(rid).strip()
                out["internal_id"] = str(d.get(extra_keys[0]) or "")
                out["nickname"] = str(d.get(extra_keys[-1]) or "")
                return out
        # 接口没给，退回读 DOM
        rid = await xhs.read_red_id(page)
        if rid:
            out["red_id"] = rid
            return out
        await asyncio.sleep(1.2)
    return out


# ---------------------------------------------------------------- 管理器

class Manager:
    def __init__(self) -> None:
        self.pw: Any = None
        self.sessions: dict[str, QrSession] = {}
        self.accounts: dict[str, dict] = {}     # key -> 账号记录
        self.user_locks: dict[str, asyncio.Lock] = {}
        self.cleaner: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        from playwright.async_api import async_playwright
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        self.accounts = load_accounts()
        self.pw = await async_playwright().start()
        self.cleaner = asyncio.create_task(self._gc_loop())
        log(f"服务就绪；已知账号 {len(self.accounts)} 个："
            f"{', '.join(a.get('user_id') or a['key'] for a in self.accounts.values()) or '-'}")

    async def stop(self) -> None:
        if self.cleaner:
            self.cleaner.cancel()
        for s in list(self.sessions.values()):
            await self._close_session(s)
        self.sessions.clear()
        if self.pw:
            try:
                await self.pw.stop()
            except Exception:
                pass

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            self._gc()

    def _gc(self) -> None:
        now = time.time()
        for key, s in list(self.sessions.items()):
            if s.status == "pending" and now > s.expires_at:
                s.status = "expired"
                s.message = f"二维码会话已超过 {ttl_text()}，请重新发起登录"
                s.qr = ""
                asyncio.create_task(self._close_session(s))

    async def _evict_if_needed(self) -> None:
        pending = [s for s in self.sessions.values() if s.status == "pending"]
        if len(pending) < MAX_SESSIONS:
            return
        pending.sort(key=lambda s: s.created_at)
        victim = pending[0]
        log(f"并发会话已满，淘汰最旧的 {victim.key}")
        victim.status = "expired"
        victim.message = "并发登录会话过多，本次已被淘汰，请重新发起"
        victim.qr = ""
        await self._close_session(victim)

    # ---------- 会话内部 ----------

    async def _close_session(self, s: QrSession) -> None:
        if s.task and not s.task.done():
            s.task.cancel()
        ctx, s.context = s.context, None
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

    def _payload(self, s: QrSession) -> dict:
        now = time.time()
        out = {
            "key": s.key,
            "status": s.status,
            "user_id": s.user_id,
            "message": s.message,
            "expires_in": max(0, int(s.expires_at - now)),
            "qr": s.qr if s.status == "pending" else "",
            "qr_updated_at": (datetime.fromtimestamp(s.qr_at).isoformat(timespec="seconds")
                              if s.qr_at else ""),
        }
        return out

    # ---------- API: 开始登录 ----------

    async def start_login(self, key: str) -> dict:
        self._gc()
        old = self.sessions.get(key)
        if old and old.status == "pending" and time.time() < old.expires_at:
            log(f"复用已有登录会话 {key}")
            return self._payload(old)

        await self._evict_if_needed()
        profile = PROFILE_DIR / key
        s = QrSession(key=key, profile=profile, created_at=time.time(),
                      expires_at=time.time() + QR_SESSION_TTL)
        try:
            ctx = await xhs.launch(self.pw, ns(profile=str(profile)))
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        except Exception as e:
            log("启动无头浏览器失败: " + traceback.format_exc())
            raise HTTPException(500, f"浏览器启动失败：{e}。请确认已执行 playwright install chromium")

        s.context, s.page = ctx, page
        self.sessions[key] = s

        try:
            await page.goto(xhs.HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2.5)
            if await xhs.is_logged_in(page, ctx):
                log(f"profile 里已有登录态，直接成功 {key}")
                await self._finish_login(s)
                return self._payload(s)
            s.qr = await qr_data_uri(page)
            s.qr_at = time.time()
            if not s.qr:
                s.message = "没取到二维码，稍后会自动重试"
            s.task = asyncio.create_task(self._poll(s))
            log(f"登录会话 {key} 已创建，等待扫码")
            return self._payload(s)
        except Exception as e:
            s.status = "error"
            s.message = f"打开登录页失败：{e}"
            log(traceback.format_exc())
            await self._close_session(s)
            return self._payload(s)

    async def _poll(self, s: QrSession) -> None:
        """后台轮询：既检查是否登录成功，也负责刷新快过期的二维码。"""
        while s.status == "pending":
            if time.time() > s.expires_at:
                s.status = "expired"
                s.message = f"二维码会话已超过 {ttl_text()}，请重新发起登录"
                s.qr = ""
                await self._close_session(s)
                return
            await asyncio.sleep(POLL_INTERVAL)
            try:
                if await xhs.is_logged_in(s.page, s.context):
                    await self._finish_login(s)
                    return
                if time.time() - s.qr_at > QR_REFRESH_INTERVAL:
                    uri = await qr_data_uri(s.page)
                    if uri:
                        s.qr, s.qr_at = uri, time.time()
                        log(f"会话 {s.key} 二维码已刷新")
            except asyncio.CancelledError:
                return
            except Exception as e:
                log(f"会话 {s.key} 轮询异常: {e}")

    async def _finish_login(self, s: QrSession) -> None:
        ident = await read_identity(s.page)
        s.user_id = ident.get("red_id") or ""
        s.status = "success"
        s.qr = ""
        if s.user_id:
            s.message = "登录成功"
        else:
            # 读不到小红书号不算登录失败：key 本身就能取数
            s.message = ("登录成功，但暂时没识别到小红书号。"
                         "可以直接用这个 key 取数据，取数时会再尝试识别并补记。")
        rec = self.accounts.get(s.key) or {}
        rec.update({
            "key": s.key,
            "user_id": s.user_id or rec.get("user_id", ""),
            "profile": str(s.profile),
            "logged_in_at": datetime.now().isoformat(timespec="seconds"),
        })
        self.accounts[s.key] = rec
        save_accounts(self.accounts)
        log(f"登录成功：key={s.key} user_id={s.user_id or '(未识别)'}")
        # 登录态已落盘到 profile，可以关掉浏览器省内存
        await asyncio.sleep(0.5)
        await self._close_session(s)

    # ---------- API: 查询状态 ----------

    def status(self, key: str) -> dict:
        s = self.sessions.get(key)
        if not s:
            raise HTTPException(404, "这个 key 不存在或已过期，请重新发起登录")
        self._gc()
        return self._payload(s)

    # ---------- API: 取笔记 ----------

    def _resolve(self, user_id: str, key: str) -> dict:
        """user_id（小红书号）和 key 二选一，谁给用谁。"""
        user_id = (user_id or "").strip()
        key = (key or "").strip()
        if key and key in self.accounts:
            return self.accounts[key]
        if user_id:
            for rec in self.accounts.values():
                if rec.get("user_id") == user_id:
                    return rec
            # 也允许把 key 当 user_id 传进来
            if user_id in self.accounts:
                return self.accounts[user_id]
        if not key and not user_id:
            raise HTTPException(400, "user_id 和 key 至少传一个")
        raise HTTPException(
            404,
            f"{'key=' + key if key else 'user_id=' + user_id} 没有对应的登录记录。"
            f"请先调用 /api/login/start 扫码登录；登录成功后 key 和 user_id 都能拿来取数。",
        )

    async def notes(self, user_id: str, key: str, start: str, end: str) -> dict:
        rec = self._resolve(user_id, key)
        profile = Path(rec["profile"])
        if not profile.exists():
            raise HTTPException(410, f"该账号的浏览器 profile 不见了（{profile}），请重新扫码登录")

        lock_key = rec.get("user_id") or rec["key"]
        lock = self.user_locks.setdefault(lock_key, asyncio.Lock())
        if lock.locked():
            raise HTTPException(429, "该账号正在抓取中，请稍后再试")

        async with lock:
            return await self._fetch_notes(rec, start, end)

    async def _fetch_notes(self, rec: dict, start: str, end: str) -> dict:
        profile = Path(rec["profile"])
        t0 = time.time()
        try:
            ctx = await xhs.launch(self.pw, ns(profile=str(profile)))
        except Exception as e:
            raise HTTPException(500, f"浏览器启动失败：{e}")
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            await page.goto(xhs.HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(2.5)
            if not await xhs.is_logged_in(page, ctx):
                raise HTTPException(401, "登录态已失效，请重新扫码登录")
            ident = await read_identity(page)
            real_id = ident.get("red_id") or ""
            if real_id and not rec.get("user_id"):
                rec["user_id"] = real_id
                self.accounts[rec["key"]] = rec
                save_accounts(self.accounts)
                log(f"补记 user_id={real_id} 到 key={rec['key']}")
            if real_id and rec.get("user_id") and real_id != rec["user_id"]:
                log(f"⚠ profile 里登录的是 {real_id}，记录里是 {rec['user_id']}")
            if not real_id and not rec.get("user_id"):
                raise HTTPException(
                    502,
                    "登录态有效，但这个账号的身份读不出来（页面接口和 DOM 都没给）。"
                    "请重试；若持续失败，说明创作者中心改版，需要更新 read_identity。",
                )

            capture = xhs.Capture(raw_dir=None, pin=None)
            page.on("response", capture.on_response)

            await xhs.goto_dashboard(page, ns())
            await asyncio.sleep(3.0)

            if start and end:
                ok = await xhs.try_set_range(page, start, end)
                log(f"时间筛选 {'已填入' if ok else '没找到输入框'}: {start} ~ {end}")
                await asyncio.sleep(2.0)

            # 翻页把 note_id 抓全
            pages = 1
            while pages < MAX_PAGES:
                before = len(capture.bodies)
                if not await xhs.goto_next_page(page):
                    break
                for _ in range(20):
                    await asyncio.sleep(0.4)
                    if len(capture.bodies) > before:
                        break
                pages += 1
                await asyncio.sleep(1.2)

            api_path, api_rows, _ = capture.merged()
            log(f"接口 {api_path or '(未命中)'} 抓到 {len(api_rows)} 条")

            # 官方导出（指标最全）
            rows: list[dict] = []
            exported = await xhs.try_export(page, EXPORT_DIR)
            if exported:
                rows = xhs.parse_official_xlsx(exported)
                by_time = {r["publish_time"]: r for r in api_rows
                           if r.get("publish_time") and r.get("note_id")}
                for r in rows:
                    m = by_time.get(r.get("publish_time"))
                    if m:
                        r["note_id"] = m["note_id"]
                log(f"官方导出 {len(rows)} 行，接口补 note_id "
                    f"{sum(1 for r in rows if r.get('note_id'))} 条")

            if not rows:
                rows = api_rows
                log("回退到接口数据（缺曝光/点击率/人均时长/弹幕）")

            uid = real_id or rec.get("user_id") or ""
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in rows:
                r["user_id"] = uid
                r["fetch_time"] = now

            rows.sort(key=lambda r: str(r.get("publish_time") or ""), reverse=True)

            return {
                "key": rec["key"],
                "user_id": uid,
                "nickname": ident.get("nickname", ""),
                "count": len(rows),
                "start": start,
                "end": end,
                "elapsed_sec": round(time.time() - t0, 1),
                "fetched_at": now,
                "source": "official_export+api" if exported else "api",
                "columns": xhs.COLUMNS,
                "notes": rows,
            }
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


manager = Manager()


# ---------------------------------------------------------------- 应用

@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.start()
    yield
    await manager.stop()


class Utf8JSONResponse(JSONResponse):
    """显式带 charset，避免某些客户端（PowerShell Invoke-RestMethod 等）
    把 UTF-8 当 Latin-1 解，中文变乱码。"""
    media_type = "application/json; charset=utf-8"


app = FastAPI(title="小红书笔记数据服务", version="1.0.0", lifespan=lifespan,
              default_response_class=Utf8JSONResponse)


KEY_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class LoginStartIn(BaseModel):
    key: str = Field(default="", description="前端生成的随机键，6~64 位字母数字")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    p = WEB_DIR / "index.html"
    if not p.exists():
        return HTMLResponse("<h1>缺少 web/index.html</h1>", status_code=500)
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.post("/api/login/start")
async def api_login_start(body: LoginStartIn) -> dict:
    key = (body.key or "").strip() or "".join(
        random.choices(string.ascii_lowercase + string.digits, k=12))
    if not KEY_RE.match(key):
        raise HTTPException(400, "key 只能是 6~64 位的字母、数字、下划线或短横线")
    return await manager.start_login(key)


@app.get("/api/login/status")
async def api_login_status(key: str = Query(..., description="登录时传入的 key")) -> dict:
    return manager.status(key)


@app.get("/api/notes")
async def api_notes(
    user_id: str = Query("", description="账号的小红书号（登录成功后返回）"),
    key: str = Query("", description="登录时前端传入的随机 key；与 user_id 二选一"),
    start: str = Query("", description="起始日期 YYYY-MM-DD，留空默认近 180 天"),
    end: str = Query("", description="结束日期 YYYY-MM-DD，留空默认今天"),
) -> dict:
    if not user_id.strip() and not key.strip():
        raise HTTPException(400, "user_id 和 key 至少传一个")
    today = date.today()
    end = end.strip() or today.isoformat()
    start = start.strip() or (today - timedelta(days=180)).isoformat()
    for d in (start, end):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            raise HTTPException(400, f"日期格式应为 YYYY-MM-DD：{d}")
    return await manager.notes(user_id.strip(), key.strip(), start, end)


@app.get("/api/notes.csv")
async def api_notes_csv(
    user_id: str = Query(""),
    key: str = Query(""),
    start: str = Query(""),
    end: str = Query(""),
) -> Response:
    data = await api_notes(user_id=user_id, key=key, start=start, end=end)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=data["columns"], extrasaction="ignore")
    w.writeheader()
    for r in data["notes"]:
        w.writerow(r)
    name = data.get("user_id") or data.get("key") or "account"
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="xhs_notes_{name}.csv"'},
    )


@app.get("/api/accounts")
async def api_accounts() -> dict:
    """已经登录过、可以直接查数据的账号列表（key 和 user_id 都能用）。"""
    return {"accounts": list(manager.accounts.values())}


@app.get("/api/health")
async def api_health() -> dict:
    pending = sum(1 for s in manager.sessions.values() if s.status == "pending")
    return {
        "ok": True,
        "pending_sessions": pending,
        "known_accounts": len(manager.accounts),
        "headless": HEADLESS,
        "qr_ttl_sec": QR_SESSION_TTL,
    }


def main() -> None:
    import uvicorn
    port = int(os.environ.get("XHS_PORT", 8320))
    uvicorn.run(app, host=os.environ.get("XHS_HOST", "0.0.0.0"), port=port, log_level="info")


if __name__ == "__main__":
    main()
