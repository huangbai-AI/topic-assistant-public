# 正式部署（无 loca.lt 验证页）

本文给你两套方案，建议先用 A（最简单）：

- A. 单平台极简：只部署 Railway（后端 + 内置 dashboard 页面）
- B. 分离部署：前端 Vercel + 后端 Railway

两套都没有 loca.lt 验证页。

## A. 最简单（只用 Railway）

1. Railway 新建项目，连接代码仓库。
2. 服务 Root Directory 设为 `backend`（已内置 `backend/Dockerfile`）。
3. 添加持久卷（Volume）挂载到 `/data`。
4. 配置环境变量（最少）：

```env
TA_API_KEY=你自己的强密码
TA_FEISHU_WEBHOOK_URL=你的飞书机器人地址
TA_CORS_ALLOW_ORIGINS=*
TA_DATA_DIR=/data
TA_COOKIE_DIR=/data/cookies
TA_DATABASE_URL=sqlite:////data/topic_assistant.db
TA_SCRAPE_HEADLESS=true
```

5. 把本地 Cookie 转 Base64，填到 `TA_XHS_COOKIES_BASE64`：

```bash
base64 -i $HOME/topic-assistant-public/backend/data/cookies/xiaohongshu.json | tr -d '\n'
```

6. 部署完成后直接访问同一个域名：
   - 健康检查：`https://<你的-backend-域名>/api/health`
   - 控制台页面：`https://<你的-backend-域名>/dashboard`（或根路径 `/`）

## 0. 先准备

1. 把项目推到 Git 仓库（GitHub/GitLab 均可）。
2. 本地确认你已有可用小红书 Cookie 文件：
   - `$HOME/topic-assistant-public/backend/data/cookies/xiaohongshu.json`

## B-1. 部署后端（Railway）

1. Railway 新建项目，选择你的代码仓库。
2. 新建服务时把 Root Directory 设为 `backend`（服务内已提供 `backend/Dockerfile`）。
3. 在 Railway 给该服务添加持久卷（Volume）并挂载到：`/data`。
4. 设置环境变量（最少）：

```env
TA_API_KEY=你自己的强密码
TA_FEISHU_WEBHOOK_URL=你的飞书机器人地址
TA_CORS_ALLOW_ORIGINS=*
TA_DATA_DIR=/data
TA_COOKIE_DIR=/data/cookies
TA_DATABASE_URL=sqlite:////data/topic_assistant.db
TA_SCRAPE_HEADLESS=true
```

5. 把本地 Cookie 转成 Base64 后填入 `TA_XHS_COOKIES_BASE64`：

```bash
base64 -i $HOME/topic-assistant-public/backend/data/cookies/xiaohongshu.json | tr -d '\n'
```

6. 部署完成后，访问：
   - `https://<你的-backend-域名>/api/health`
   - 返回 `{"ok":true,...}` 即成功。

## B-2. 部署前端（Vercel）

1. Vercel 导入同一个仓库。
2. Project Root 设为 `frontend`。
3. 环境变量里配置：

```env
VITE_API_BASE=https://<你的-backend-域名>
```

4. 部署后打开前端页面，进入「后台配置」确认 `API Key`、Webhook。

## 3. 生产建议

1. 后端稳定后，把 `TA_CORS_ALLOW_ORIGINS` 从 `*` 改为你的前端正式域名（可逗号分隔多个）。
2. Railway/Vercel 都绑自定义域名，避免平台随机域名变化。
3. 若 Cookie 过期，重新登录抓取一次并更新 `TA_XHS_COOKIES_BASE64`，然后重启后端服务。

## 4. 本次代码里已经帮你加好的部署能力

1. 新增后端健康检查接口：`/api/health`
2. 新增后端 Docker 镜像配置（含 Playwright 运行环境）：
   - `backend/Dockerfile`
   - `backend/.dockerignore`
3. 新增前端 Vercel 配置：
   - `frontend/vercel.json`
4. 新增“通过环境变量注入 Cookie”能力：
   - `TA_XHS_COOKIES_JSON`
   - `TA_XHS_COOKIES_BASE64`
   - `TA_XHS_COOKIES_OVERWRITE`
