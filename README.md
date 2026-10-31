# 选题助手 · 脱敏公开版

按关键词搜索小红书、B站内容，结合互动量、时间等条件筛选参考，再按配置推送到飞书。适合为创作者建立选题候选池；不是所有平台都已接入，也不承诺无人值守完成登录或验证。

这是独立公开版，只包含代码与配置模板；原版项目和原有数据另行保留，不随本仓库发布。使用者需要自行安装、登录平台、配置自己的飞书及 API Key。请先阅读 [安全与隐私](SECURITY.md)。

## 从零开始

需要 Python 3.10 或更新版本。以下命令适用于 macOS / Linux：

```bash
git clone https://github.com/huangbai-AI/topic-assistant-public.git
cd topic-assistant-public
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
python -m playwright install chromium
cp backend/.env.example backend/.env
# 先编辑 backend/.env：把 TA_API_KEY 改成自己的强口令，按需填写飞书配置。
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/dashboard`。Windows 使用 `.venv\\Scripts\\Activate.ps1` 激活虚拟环境；平台采集可能仍需在本机完成扫码登录。涉及登录、验证码或访问限制时请停止采集并按平台要求处理。

下方保留各组件和部署方式的详细说明；不同小节的相对路径均需从仓库根目录开始使用。


项目已拆分为前后端独立开发模式：
- `backend`：FastAPI + 抓取调度 + 数据存储 + 推送
- `frontend`：独立前端壳（统一重定向到后端 `/dashboard`，避免双实现分叉）

正式线上部署请看：`DEPLOY.md`

版本管理：
- 当前开发版本：`VERSION`
- 版本记录：`CHANGELOG.md`（旧的私有运行快照不随公开版分发）

## 功能

- **自动抓取**：定时搜索配置的关键词，按热度排序，提取爆款内容
- **飞书推送**：每日定时推送消息卡片，包含各平台 Top 内容
- **手动收藏**：通过 API 提交链接，自动入库（供浏览器插件 / iOS 快捷指令调用）
- **REST API**：查询已收藏内容、管理关键词配置

## 快速开始（后端）

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入飞书 Webhook URL 等配置（含 TA_CORS_ALLOW_ORIGINS）
# 如需把“登录掉线告警二维码截图”直接发到飞书，请补充：
# TA_FEISHU_APP_ID=...
# TA_FEISHU_APP_SECRET=...
# 如需在飞书告警里直接点开“二维码截图/HTML快照”：
# TA_PUBLIC_BASE_URL=https://your-topic-assistant.example.com
```

### 3. 小红书登录（首次需要）

```bash
cd backend
python -m scrapers.xiaohongshu
```

会打开浏览器，手动扫码登录小红书后按 Enter 保存 Cookie。

### 4. 添加关键词

```bash
curl -X POST http://localhost:8000/api/keywords \
  -H "Content-Type: application/json" \
  -H "X-API-Key: changeme" \
  -d '{"keyword": "AIGC", "platforms": ["xiaohongshu"]}'
```

### 5. 启动服务

```bash
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000
```

服务启动后，定时任务会在每天配置的时间自动执行抓取+推送。

### 一键启动云端访问（后端 + Tunnel）

```bash
cd $HOME/topic-assistant-public
./scripts/topic_cloud.sh start
```

常用命令：

```bash
./scripts/topic_cloud.sh status
./scripts/topic_cloud.sh restart
./scripts/topic_cloud.sh stop
```

### 开机自启（macOS launchd）

```bash
cd $HOME/topic-assistant-public
./scripts/launchd_manage.sh install
./scripts/launchd_manage.sh status
```

说明：
- `install` 会托管 3 个服务：`backend`、`tunnel`、`frontend`
- 日志路径：
  - `/tmp/topic_backend.launchd.out.log`
  - `/tmp/topic_tunnel.launchd.out.log`
  - `/tmp/topic_frontend.launchd.out.log`

## 快速开始（前端）

```bash
cd frontend
npm install
npm run dev
```

浏览器打开：

```text
http://localhost:5173
```

说明：
- 默认读取 `frontend/.env` 中的 `VITE_API_BASE`（示例见 `frontend/.env.example`）
- 页面会把 `apiBase` 保存到 `localStorage`，之后可在“后台配置”页修改
- 也支持 URL 参数临时覆盖：`?apiBase=http://localhost:8000`

### 一键本地联调（推荐）

前后端一起启动，避免“只起一半”的状态漂移：

```bash
cd $HOME/topic-assistant-public
./scripts/topic_local.sh start
```

常用命令：

```bash
./scripts/topic_local.sh status
./scripts/topic_local.sh restart
./scripts/topic_local.sh stop
./scripts/topic_local.sh logs
```

### 6. 手动触发一次抓取

```bash
curl -X POST http://localhost:8000/api/scrape/trigger \
  -H "X-API-Key: changeme"
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/collect` | 手动收藏一条内容 |
| GET | `/api/contents` | 查询已收藏的内容列表 |
| GET | `/api/platforms` | 查询当前后端支持的平台 |
| GET | `/api/feed` | 刷流页数据（支持关键词/点赞/时间筛选） |
| POST | `/api/feed/{id}/read` | 标记已读 |
| POST | `/api/feed/{id}/star` | 标记/取消标星 |
| GET | `/api/feed/{id}/resolve-video` | 按需解析视频链接（小红书） |
| GET | `/api/feed/{id}/stream` | 视频流代理（同域播放） |
| POST | `/api/keywords` | 添加/更新关键词 |
| GET | `/api/keywords` | 查看所有关键词 |
| DELETE | `/api/keywords/{id}` | 删除关键词 |
| POST | `/api/scrape/trigger` | 手动触发抓取（入队） |
| GET | `/api/tasks` | 查询任务列表（queued/running/success/failed） |
| GET | `/api/tasks/{task_id}` | 查询任务详情 |
| GET | `/api/runtime/status` | 运行时状态（后端/调度/任务 runner/公网探针） |
| GET | `/api/risk/events/{id}/artifact-token` | 获取风控附件短时 token |
| GET | `/api/risk/events/{id}/artifact?kind=...&token=...` | 读取风控附件（推荐 token 鉴权） |

`POST /api/scrape/trigger` 返回示例：

```json
{
  "detail": "Scrape job triggered in background",
  "task_id": "cdb8f8b0c98a49f3bc8bc6ddef79b4cc",
  "status": "queued",
  "queued_at": "2026-02-22T16:30:00.123456",
  "position": 1,
  "scope": "selected",
  "keyword_count": 2,
  "scrape_headless": true
}
```

错误返回统一结构：

```json
{
  "code": "AUTH_INVALID",
  "message": "Invalid API key",
  "hint": "请检查 API Key 是否正确，并确认前端已同步最新配置。",
  "request_id": "a1b2c3d4e5f67890"
}
```

## 扩展新平台（本地开发）

1. 在 `backend/scrapers/` 新增平台 scraper，继承 `BaseScraper` 并实现 `search`。
2. 在 `backend/scrapers/registry.py` 注册平台名和类工厂。
3. 重启后端后，`GET /api/platforms` 会自动反映新平台。

## V1.1 刷流页面（实验）

- 入口：`http://127.0.0.1:8000/feed`
- 目标：把筛选后的结果做成可连续浏览的“精选流”
- 说明：视频播放依赖可解析到视频直链；若直链不可用会自动回退封面预览 + 原帖打开

## 目录结构

```
frontend/
├── index.html            # 页面结构
├── src/main.js           # 页面逻辑
└── package.json          # Vite 工程配置

backend/
├── main.py              # FastAPI 入口 + API 路由
├── config.py            # 配置管理（环境变量）
├── database.py          # 数据库模型
├── models.py            # Pydantic 数据模型
├── scheduler.py         # 定时任务调度
├── scrapers/
│   ├── base.py          # Scraper 基类
│   └── xiaohongshu.py   # 小红书 Scraper
├── push/
│   └── feishu.py        # 飞书推送
├── data/                # 数据目录（自动创建）
│   ├── cookies/         # 平台登录 Cookie
│   └── topic_assistant.db  # SQLite 数据库
└── .env                 # 环境变量配置
```
