# 前端控制台（独立开发）

这是选题助手的独立前端页面（Vite 工程）。

## 启动

```bash
cd frontend
npm install
npm run dev
```

默认打开：

```text
http://localhost:5173
```

如需前后端一起稳定拉起（推荐）：

```bash
cd $HOME/topic-assistant-public
./scripts/topic_local.sh start
```

## 环境变量

- 复制 `.env.example` 为 `.env`，可配置默认后端地址：

```bash
cp .env.example .env
```

```env
VITE_API_BASE=http://localhost:8000
```

## API 地址配置

- 支持通过 URL 参数覆盖：`?apiBase=http://localhost:8000`
- 首次加载会写入 `localStorage.ta_api_base`
- 也可以在页面「后台配置」中修改并保存

## 说明

- 前端调用后端 API 时会带 `X-API-Key`
- 后端需要配置 `TA_CORS_ALLOW_ORIGINS` 允许前端域名
- 生产构建命令：`npm run build`
