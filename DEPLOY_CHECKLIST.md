# 部署清单（先本地调试，再上云）

适用目标：先把功能在本地调好，再做正式部署。

推荐架构（最省事）：
- 代码放 GitHub
- 后端常驻在腾讯云轻量（中国香港）
- 前端先使用后端内置页面（`/dashboard`、`/feed`）

预计成本：
- 服务器约 `¥40~60/月`（2核2G 常见档位）
- 域名可选（约 `¥50~100/年`）

---

## 1. 上云前准备

1. 确保本地后端功能已验证通过（抓取、筛选、飞书推送）。
2. 确保本地有可用 Cookie 文件：
   - `$HOME/topic-assistant-public/backend/data/cookies/xiaohongshu.json`
3. 代码推送到 GitHub 仓库。
4. 准备飞书机器人 Webhook。

---

## 2. 购买云服务器

1. 购买腾讯云轻量服务器（建议 `Ubuntu 22.04`，`2核2G`）。
2. 安全组放行端口：`22`、`80`、`443`（`8000` 可先临时放行用于调试）。

---

## 3. 初始化服务器

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install git docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

---

## 4. 拉取项目

```bash
cd /opt
sudo git clone <你的仓库地址> topic-assistant
sudo chown -R $USER:$USER /opt/topic-assistant
cd /opt/topic-assistant/backend
```

---

## 5. 配置后端环境变量

先在本地把 Cookie 转为 Base64：

```bash
base64 -i $HOME/topic-assistant-public/backend/data/cookies/xiaohongshu.json | tr -d '\n'
```

在服务器 `/opt/topic-assistant/backend/.env` 填入：

```env
TA_API_KEY=换成强密码
TA_FEISHU_WEBHOOK_URL=你的飞书Webhook
TA_CORS_ALLOW_ORIGINS=*
TA_DATA_DIR=/data
TA_COOKIE_DIR=/data/cookies
TA_DATABASE_URL=sqlite:////data/topic_assistant.db
TA_SCRAPE_HEADLESS=true
TA_XHS_COOKIES_BASE64=上一步生成的一整串
```

---

## 6. 启动后端（常驻）

```bash
cd /opt/topic-assistant/backend
docker build -t topic-assistant-backend .
docker run -d \
  --name topic-assistant-backend \
  --restart unless-stopped \
  -p 8000:8000 \
  -v /opt/topic-assistant/data:/data \
  --env-file .env \
  topic-assistant-backend
```

---

## 7. 验证服务

```bash
curl http://127.0.0.1:8000/api/health
```

页面访问：
- `http://<服务器IP>:8000/dashboard`
- `http://<服务器IP>:8000/feed`

---

## 8. 设置并验证定时抓取

1. 在 dashboard 中设置并开启定时（`/api/schedule`）。
2. 观察日志确认任务触发：

```bash
docker logs -f topic-assistant-backend
```

出现 `Scrape job started` 代表定时任务已执行。

---

## 9. 生产化建议（上线后）

1. 配置反向代理 + HTTPS（Nginx/Caddy + 证书）。
2. 将 `TA_CORS_ALLOW_ORIGINS` 从 `*` 改为正式前端域名。
3. Cookie 失效时更新 `TA_XHS_COOKIES_BASE64` 并重启：

```bash
docker restart topic-assistant-backend
```

