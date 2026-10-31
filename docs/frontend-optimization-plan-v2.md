# 前端优化计划（React 化 + 稳定性优先 + 渐进迁移）

## 摘要
本计划将当前单文件 Dashboard 前端升级为 `React + TypeScript + MUI` 架构，采用“并行发布、平滑切换”策略。  
已锁定决策如下：  
1. 迁移策略：渐进迁移。  
2. UI 方案：MUI + 主题定制（Material Design 风格）。  
3. 切换门槛：并行一周且零阻塞 Bug 后切默认入口。  
4. 第一阶段范围：Dashboard 全量模块一起迁移（任务、历史、配置）。  
5. API 策略：保持现有后端 API 优先，仅做向后兼容增补。  
6. 设备策略：桌面优先，移动端保证可用。  
7. Feed 页面：第二阶段纳入 React 改造。

---

## 现状基线（基于仓库真实状态）
1. 主前端为单文件：`$HOME/选题插件开发/backend/static/dashboard.html`（1552 行）。  
2. `frontend` 当前是跳转壳：`$HOME/选题插件开发/frontend/src/main.js` 仅重定向到 `/dashboard`。  
3. 页面状态、DOM 操作、API 调用强耦合，主要痛点是状态漂移和回归难。  
4. 后端 API 已比较完整，适合前端先平滑替换，不必先重构后端。

---

## 目标与范围
1. 目标  
1. 提升可维护性：从“单文件脚本”升级到“组件化 + 类型化 + 可测试”。  
2. 提升稳定性：消除模式错乱、触发链路状态不一致、错误提示不清。  
3. 提升体验：统一视觉、交互反馈、任务状态可观测性。

2. 本轮范围（Dashboard v2）  
1. 任务页：手动触发弹窗、关键词选择/预设、定时任务、任务中心。  
2. 历史页：任务记录、风控记录、详情弹层。  
3. 配置页：API Key、飞书配置、抓取模式、运行状态灯。  
4. 保留旧版 `/dashboard` 并新增 `/dashboard-v2` 并行运行。

3. 本轮不做  
1. 不先重构后端核心业务逻辑。  
2. 不在第一阶段迁移 `/feed`。  
3. 不引入多前端框架并存。

---

## 技术方案（决策完成）
1. 前端栈  
1. `React 18 + TypeScript + Vite`  
2. `MUI`（Material 风格组件 + 主题系统）  
3. `TanStack Query`（服务端状态）  
4. `Zustand`（UI 局部状态：弹窗、筛选、预设草稿）  
5. `React Hook Form + Zod`（配置表单校验）  
6. `dayjs`（时间格式化）  
7. `Vitest + Testing Library + Playwright`（单测 + E2E）

2. 目录结构（目标）
1. `$HOME/选题插件开发/frontend/src/app`：应用壳、路由、主题。  
2. `$HOME/选题插件开发/frontend/src/features/tasks`：任务相关页面与组件。  
3. `$HOME/选题插件开发/frontend/src/features/history`：历史与风控。  
4. `$HOME/选题插件开发/frontend/src/features/config`：系统配置。  
5. `$HOME/选题插件开发/frontend/src/shared/api`：API client 与类型。  
6. `$HOME/选题插件开发/frontend/src/shared/components`：通用组件。  
7. `$HOME/选题插件开发/frontend/src/shared/state`：Zustand store。  
8. `$HOME/选题插件开发/frontend/src/shared/utils`：格式化与工具。

3. 状态与数据流规则  
1. 所有 `/api/*` 读请求走 Query，统一缓存键与轮询策略。  
2. 所有写请求走 Mutation，成功后精准失效相关 Query。  
3. “触发弹窗临时选择”仅保存在本地 UI Store。  
4. “用户最近选择/预设”保留 `localStorage`，并提供 schema 版本字段。  
5. “抓取模式展示”以 `/api/system-config` 为唯一真值，不在多处复制。

---

## 页面信息架构与组件拆分
1. 顶层布局  
1. `AppShell`：标题栏、全局状态灯、主 Tab。  
2. `TaskTab`、`HistoryTab`、`ConfigTab` 三大页。  

2. 任务页组件  
1. `ManualTriggerButton`（右上角触发入口）。  
2. `TriggerConfirmDialog`（模式、关键词范围、预设、摘要确认）。  
3. `ScheduleCard`（多时间点编辑）。  
4. `KeywordsTableCard`（关键词 CRUD）。  
5. `TaskQueueCard`（队列、执行中、失败分类、详情跳转）。

3. 历史页组件  
1. `HistorySubTabs`（任务记录 / 风控记录）。  
2. `HistoryTable`（分页、筛选、详情）。  
3. `RiskStatsPanel`（统计与刷新）。  
4. `RiskEventsDialog`（风控事件列表与附件链接）。

4. 配置页组件  
1. `SystemConfigForm`（API Key、飞书 webhook/chat_id、抓取模式）。  
2. `RuntimeStatusBar`（backend/scheduler/runner/public 健康）。  
3. `SaveResultToast`（统一成功/失败反馈）。

---

## API 与接口变更（保持兼容）
1. 现有 API 继续复用，不做破坏性修改。  
2. 建议新增的可选字段（向后兼容）  
1. `GET /api/tasks` 与 `GET /api/tasks/{task_id}` 增加 `effective_headless`、`headless_source`。  
2. `GET /api/dashboard/bootstrap` 增加 `ui_capabilities`（如 `supports_task_mode_runtime_switch`）。  
3. 错误返回保持现结构 `{code,message,hint,request_id}`，前端统一映射文案。

3. 前端类型约束  
1. 在 `$HOME/选题插件开发/frontend/src/shared/api/types.ts` 定义全部响应类型。  
2. 严禁 `any` 直读业务字段。  
3. 所有枚举状态统一为字面量联合类型。

---

## 分阶段实施计划（5-7 天）
1. 阶段 A（D1-D2）：工程与骨架  
1. 初始化 React TS 工程结构与 MUI 主题。  
2. 建立 API client、QueryClient、全局错误处理。  
3. 搭建三大页骨架与路由。  
4. 接入 `/api/dashboard/bootstrap` 初始化流程。  
5. 输出可访问入口 `/dashboard-v2`（不替换旧入口）。

2. 阶段 B（D3-D4）：Dashboard 全量模块迁移  
1. 迁移任务页完整功能。  
2. 迁移历史页与风控页完整功能。  
3. 迁移配置页完整功能。  
4. 完成原页面所有关键交互对齐。  
5. 完成错误提示分级与 loading/empty/error 三态。

3. 阶段 C（D5-D7）：稳定性与切换准备  
1. 完成单测与 E2E 核心用例。  
2. 灰度并行一周观察。  
3. 修复并行期间问题。  
4. 达成“零阻塞 Bug”后将默认入口切到 `/dashboard-v2`。  
5. 旧版 `/dashboard` 保留为回滚入口一周。

---

## 详细验收标准
1. 功能一致性  
1. 任务、历史、配置三页功能与旧版等价。  
2. 手动触发弹窗支持关键词增改、预设、模式确认。  
3. 触发后任务状态可追踪，失败原因可见。  

2. 稳定性  
1. 连续操作 30 分钟无白屏、无未捕获异常。  
2. 轮询与手动刷新无重复渲染卡顿。  
3. API 失败时提示可定位，不出现“静默失败”。  

3. 体验  
1. 统一 Material 风格，控件间距与对齐一致。  
2. 桌面宽屏体验优先，移动端关键流程可用。  
3. Tab、表单、弹窗交互响应清晰，避免误触发。

---

## 测试用例与场景
1. 单元测试  
1. 触发弹窗模式切换与关键词选择逻辑。  
2. 预设保存/应用/删除与 localStorage 兼容。  
3. 错误码映射与提示文案。  

2. 集成测试  
1. `/api/scrape/trigger` 成功后任务列表刷新。  
2. `/api/system-config` 保存后模式展示同步。  
3. 历史筛选条件变化后数据正确更新。  

3. E2E（Playwright）  
1. “触发任务 -> 查看任务状态 -> 历史记录可见”闭环。  
2. “登录锁定时触发失败提示”路径。  
3. “风控记录打开附件链接”路径。

---

## 发布、监控与回滚
1. 发布策略  
1. 新增路由 `/dashboard-v2`，旧 `/dashboard` 不动。  
2. 并行观察 7 天后切默认入口。  

2. 监控指标  
1. 前端 JS 运行错误数。  
2. 关键 API 失败率（触发、任务、配置、历史）。  
3. 用户关键路径成功率（触发成功、任务可见、配置保存成功）。  

3. 回滚策略  
1. 任一阻塞问题出现即恢复默认 `/dashboard`。  
2. `/dashboard-v2` 保留用于修复验证。  
3. 不改动后端核心抓取逻辑，回滚成本低。

---

## 第二阶段（Feed 迁移）
1. 把 `/feed` 迁移到 React 子应用页面。  
2. 统一与 Dashboard 的主题、状态管理与 API client。  
3. 保持“视频流代理、已读/标星、筛选”能力不回退。  
4. 迁移完成后再评估是否拆分成独立前端包。

---

## 假设与默认
1. 默认继续使用当前后端和 API 结构，不做大规模后端重构。  
2. 默认继续使用现有鉴权方式（`X-API-Key`）。  
3. 默认部署方式维持当前公网入口与 Cloudflare 隧道链路。  
4. 默认以“稳定性与回归可控”优先于“短期视觉重做幅度”。
