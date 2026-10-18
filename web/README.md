# aegra-web

aegra-api（Deep Agents supervisor + 领域子 agent）的前端，放在主仓库 `web/` 下。Vue 3 + Vite + TypeScript + Pinia。

界面：左侧会话列表 / 中间对话 / 右侧面板（子任务进度、知识库文件树、定时任务）。

## 跑起来

```bash
npm install
npm run dev          # http://localhost:5273
```

后端要先起（本仓库根目录）：

```bash
cd ..
PYTHONPATH=. uv run python -u src/aegra_api/main.py    # :2026
```

默认账号 `root / 1234`（见后端 `custom_test.py` 的说明）。后端地址用 `.env` 覆盖：

```bash
cp .env.example .env
```

## 契约冒烟测试

不改 UI、只验证前后端接口形状是否对得上（登录 → 建会话 → 流式 → 追问 resume →
任务代理 → 定时任务 → 删会话，20 个断言）：

```bash
node scripts/smoke.mjs
AEGRA_API=http://127.0.0.1:2026 AEGRA_USER=root AEGRA_PASS=1234 node scripts/smoke.mjs
```

## 结构

```
src/
├── api/client.ts      fetch 封装（token / 401 / SSE 解析）
├── api/endpoints.ts   与后端接口一一对应
├── stores/chat.ts     会话、流式、追问、子任务、轮询
├── components/        LoginView / ThreadList / ChatView / MessageItem / InterruptCard / SidePanel
└── utils/markdown.ts  marked + DOMPurify
```

## 四个必须知道的实现点

1. **`messages/partial` 是全量文本**：每次收到都要按消息 id 算增量，不能直接追加。
   流式期间先写 `streaming`，`end` 后统一以 `GET /threads/{id}/state` 刷新为准。
2. **追问是 interrupt**：`updates` 帧里出现 `__interrupt__` → 渲染选项 → `POST /threads/{id}/runs`
   带 `{"command":{"resume":"<label>"}}`。刷新页面时用 `state.interrupts` 恢复。
3. **子 agent 回写不是用户消息**：父线程里那条 `human` 消息以 `[Async subagent '...' has completed]`
   开头，渲染成单独样式（`role=callback`）。
4. **子 agent 的流要单独订阅**：`GET /custom/tasks/{id}/stream?parent_thread_id=...`。
   不要直接读父 state 的 `async_tasks.status`（会滞后），要 `GET /custom/tasks/{id}`。

接口全文见后端 [`../docs/frontend-api.md`](../docs/frontend-api.md)。
