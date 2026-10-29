# 小红书笔记数据采集器（自有账号）

> 用你自己的小红书账号登录，把**创作者中心**里的笔记数据完整导出成表格，同时提供一套 HTTP API。
> 不需要开放平台权限，也不需要购买第三方数据接口。

A self-hosted collector + HTTP API that exports note statistics from **your own**
Xiaohongshu (RED) creator dashboard. Headless Playwright, no signature reverse-engineering.

---

## 这是什么

小红书创作者中心里，你能看到每篇笔记的曝光、观看、封面点击率、点赞、评论、收藏、涨粉、分享、
人均观看时长和弹幕。但这些数据只存在于后台页面里，没有一条给程序调用的接口。

这个项目把「人用浏览器看后台」这件事自动化掉：

- **Web 服务**：扫码登录 → 轮询登录状态 → 用 `user_id` 或 `key` 拉全量笔记数据，附一个单页前端
- **命令行工具**：同样的能力，适合放到 cron / 定时任务里
- 产出 Excel + CSV，或者直接返回 JSON

## 为什么不用现成的方案

| 方案 | 问题 |
|---|---|
| 小红书开放平台 | 面向专业号没有开放「笔记数据」接口，开放能力偏电商和内容发布 |
| 第三方数据接口 | 能拿到点赞/收藏/评论，但**拿不到曝光、封面点击率、人均观看时长、弹幕**；且按次计费 |
| 自己逆向签名算法 | 前端一改版就挂，维护成本极高 |
| **本项目** | 登录态是你自己的，签名由官方前端生成，字段和后台完全一致，免费 |

## 工作原理

核心思路只有一句话：**不自己算签名，让页面自己发请求，我们只监听响应。**

```
Playwright（无头，持久化 profile 保住登录态）
  └─ 打开 creator.xiaohongshu.com/statistics/data-analysis
      ├─ 页面自己发出带 x-s / x-s-common 签名的 XHR → 监听响应拿 JSON（含 note_id）
      └─ 点击页面上的「导出数据」→ 拿官方 xlsx（指标最全）
          └─ 按「发布时间」合并两路数据 → Excel / CSV / JSON
```

因为签名头永远由官方前端现场生成，所以前端改版最多影响接口路径，不影响算法 —— 没有需要逆向的东西。

## 功能特性

- **全程无头**，不渲染任何浏览器界面
- **二维码自动刷新**：小红书二维码本身约 3 分钟失效，服务端每 45 秒重取一次，
  登录会话 30 分钟有效，用户任何时候看到的码都是能扫的
- **登录成功即释放浏览器**：登录态由 Playwright 持久化 profile 落盘，取数时再启，
  省内存，服务重启登录态也不丢
- **一个 key 一个 profile**，多账号互不干扰
- **两条数据源合并**：官方导出给全指标，数据接口给 `note_id`，互补
- **取数入口支持 `user_id`（小红书号）或登录 `key`**，二选一
- 附带 CLI：`login` / `check` / `probe` / `collect`，可挂定时任务

## 快速开始

要求：Python 3.10+、能访问 `creator.xiaohongshu.com` 的网络环境。

```bash
git clone <repo-url>
cd xhs_self_collector

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 只装一次：下载 Playwright 自带的 Chromium
playwright install chromium
# Linux 额外装系统依赖
playwright install-deps chromium

# 启动 Web 服务
python app.py
```

打开 <http://127.0.0.1:8320/>，输入（或自动生成）一个 key，点「发起登录」，
用手机小红书扫码，登录成功后填 `user_id` 或刚那个 `key`，点「拉取笔记数据」。

> 如果服务起在别的机器上，二维码图片会作为 data URI 直接在网页里显示，扫码端不需要能访问服务器。

## Web 服务

### API

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/login/start` | body `{"key": "任意 6~64 位字符串"}`。启动无头浏览器、打开登录页、取二维码，返回 `qr`（data URI）、`status`、`expires_in`。该 key 30 分钟内有效 |
| `GET` | `/api/login/status?key=…` | 轮询登录状态。`status` ∈ `pending` / `success` / `expired` / `error`；成功时返回 `user_id`；`pending` 时每次都带最新的二维码 |
| `GET` | `/api/notes` | 取笔记数据。参数 `user_id` **或** `key`（二选一），可选 `start` / `end`（`YYYY-MM-DD`，默认近 180 天） |
| `GET` | `/api/notes.csv` | 同上，直接下载 CSV |
| `GET` | `/api/accounts` | 已登录过、可直接查数据的账号列表 |
| `GET` | `/api/health` | 健康检查 |

### 调用示例

```bash
# 1. 发起登录
curl -X POST http://127.0.0.1:8320/api/login/start \
     -H 'Content-Type: application/json' \
     -d '{"key":"my-login-001"}'

# 2. 轮询（把返回的 qr 显示出来让人扫）
curl 'http://127.0.0.1:8320/api/login/status?key=my-login-001'

# 3. 取数据 —— 两种入口都行
curl 'http://127.0.0.1:8320/api/notes?user_id=123456789'
curl 'http://127.0.0.1:8320/api/notes?key=my-login-001&start=2026-01-01&end=2026-06-30'

# 4. 下载 CSV
curl -O -J 'http://127.0.0.1:8320/api/notes.csv?key=my-login-001'
```

`/api/login/status` 返回：

```json
{
  "key": "my-login-001",
  "status": "pending",
  "user_id": "",
  "message": "",
  "expires_in": 1788,
  "qr": "data:image/png;base64,iVBORw0KGgo...",
  "qr_updated_at": "2026-10-29T14:59:01"
}
```

`/api/notes` 返回：

```json
{
  "key": "my-login-001",
  "user_id": "123456789",
  "nickname": "示例账号",
  "count": 2,
  "start": "2026-01-01",
  "end": "2026-06-30",
  "source": "official_export+api",
  "elapsed_sec": 17.4,
  "fetched_at": "2026-10-29 15:26:24",
  "columns": ["user_id", "note_id", "title", "publish_time", "type"],
  "notes": [
    {
      "user_id": "123456789",
      "note_id": "6ae2eacd000000002a024feb",
      "title": "示例标题",
      "publish_time": "2026-10-29 14:42:21",
      "type": "图文",
      "liked_count": 1,
      "collected_count": 1,
      "comments_count": 0,
      "share_count": 0,
      "impressions": 0,
      "views": 5,
      "ctr": 0,
      "follows": 0,
      "avg_watch_sec": 0,
      "danmaku": 0,
      "fetch_time": "2026-10-29 15:26:24"
    }
  ]
}
```

`source` 的取值：

- `official_export+api` —— 官方导出成功，字段最全
- `api` —— 回退到接口数据，缺曝光 / 封面点击率 / 人均观看时长 / 弹幕

### 前端

`web/index.html`，无框架单文件。输入或生成随机 key → 显示二维码并每 2 秒轮询 →
成功后显示 `user_id` → 在取数框填 `user_id` 或 `key` → 渲染表格 + 下载 CSV。

## 输出字段

| 列 | 含义 | 来源 |
|---|---|---|
| `user_id` | 小红书号 | 账号身份 |
| `note_id` | 笔记 ID | 数据接口 |
| `title` | 标题 | 官方导出 |
| `publish_time` | 首次发布时间 `YYYY-MM-DD HH:MM:SS` | 两者都有 |
| `type` | 体裁（图文 / 视频） | 官方导出 |
| `impressions` | 曝光 | 官方导出 |
| `views` | 观看量 | 官方导出 |
| `ctr` | 封面点击率 | 官方导出 |
| `liked_count` | 点赞 | 两者都有 |
| `collected_count` | 收藏 | 两者都有 |
| `comments_count` | 评论 | 两者都有 |
| `share_count` | 分享 | 两者都有 |
| `follows` | 涨粉 | 官方导出 |
| `avg_watch_sec` | 人均观看时长 | 官方导出 |
| `danmaku` | 弹幕 | 官方导出 |
| `fetch_time` | 抓取时间 | 本地生成 |

数字统一转成纯数值（`1.2万` → `12000`）。

## 配置

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `XHS_PORT` | `8320` | 监听端口 |
| `XHS_HOST` | `0.0.0.0` | 监听地址 |
| `XHS_DATA_DIR` | `./data` | profile、账号表、导出文件的存放目录 |
| `XHS_QR_TTL` | `1800` | 登录会话有效期（秒） |
| `XHS_MAX_SESSIONS` | `4` | 同时进行中的登录会话上限，超出会淘汰最旧的 |
| `XHS_MAX_PAGES` | `60` | 取数时最多翻几页 |

运行时状态都在 `XHS_DATA_DIR` 下：

```
data/
├── profiles/<key>/     # 每个登录 key 一个浏览器 profile（含登录态 cookie）
├── users.json          # 账号表，以 key 为主键
└── exports/            # 官方导出的原始 xlsx
```

`users.json` 结构（旧版 `user_id -> record` 格式启动时会自动迁移）：

```json
{
  "version": 2,
  "accounts": {
    "<key>": { "key": "…", "user_id": "…", "profile": "…", "logged_in_at": "…" }
  }
}
```

> `data/` 里存着登录态 cookie，`.gitignore` 已排除。**不要提交、不要外发。**

## 命令行

不想要 Web 服务时，可以直接用 CLI：

```bash
# 登录：出二维码 PNG，扫码后把登录态写进 profile
python xhs_creator_collect.py login --qr-out out/qr.png

# 检查登录态（退出码 0 有效 / 3 失效）
python xhs_creator_collect.py check

# 探针：列出数据页的接口路径与字段名，前端改版后用它重新对一遍
python xhs_creator_collect.py probe --dashboard-url https://creator.xiaohongshu.com/statistics/data-analysis

# 采集并导出
python xhs_creator_collect.py collect \
    --start 2026-01-01 --end 2026-06-30 \
    --export --out out/notes.xlsx
```

公共参数：`--profile` 指定 profile 目录（一个账号一个）、`--headful` 有头模式、
`--no-sandbox`（root 跑 Chromium 时用）、`--pin-url` 锁定只捕获某个接口。

Linux 服务器上如果纯无头不稳定，可以装 xvfb 跑有头模式：

```bash
xvfb-run -a python xhs_creator_collect.py collect --headful --no-sandbox ...
```

## 已知限制

- **只能看到近半年发布的笔记**，这是创作者中心数据页本身的限制
- **官方导出单次最多 1000 条**，超过的部分只能靠接口分页补
- 拿不到笔记正文（`desc`）和 IP 属地，这两个字段不在数据页上
- **登录态几天到几周会过期**，需要重新扫码；建议配一条 `check` 告警
- 单机串行：同一个账号同时只允许一个抓取任务，避免并发触发风控
- 接口路径和字段名随官方前端改版可能变化，届时需要跑一次 `probe` 重新确认

## 目录结构

```
xhs_self_collector/
├── app.py                   # FastAPI 服务（三个 API + 会话管理）
├── web/index.html           # 单页前端
├── xhs_creator_collect.py   # 采集内核（CLI + 供 app.py 复用）
├── requirements.txt
├── .gitignore
├── test_api.py              # API 冒烟测试
├── test_migration.py        # 账号表格式迁移测试
├── webtest.py               # 服务端到端自测（可选生成前端截图）
├── data/                    # 运行时状态（不提交）
└── out/                     # 产物（不提交）
```

## 开发与测试

```bash
# 不联网：验证数字转换、去重、时间解析、Excel/CSV 导出
python xhs_creator_collect.py selftest

# 账号表格式迁移
python test_migration.py

# API 冒烟（需要服务已启动，并设置 XHS_TEST_IDENT 为已登录的 user_id 或 key）
set XHS_TEST_IDENT=<your-user-id-or-key>
python test_api.py
```

## 免责声明

本项目仅用于**导出并分析你自己账号**的数据，例如做经营复盘、内容迭代、报表归档。

- 请只使用你自己的账号登录，不要用它抓取他人数据
- 请保持低频使用，不要并发、不要做成对外提供数据的服务或产品
- 不要把 `data/` 目录（含登录态）提交到仓库或发给他人
- 自动化访问可能违反平台用户协议，账号受限的风险由使用者自行承担
- 本项目与小红书官方无关，未获得其授权或背书

如果你需要的是对外产品级数据能力，请走小红书官方开放平台或商务合作渠道。

## 许可

MIT
