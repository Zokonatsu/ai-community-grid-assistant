# 跨会话产品协同 · 协调机制

两个 Agent：**产品规划（planner）** + **开发（developer）**，借助 Codex 跨会话消息自动协作，验收循环全程无需用户介入。

## 目录
- `agent-registry.md` — 身份注册表（职责 + 会话 ID）
- `tasks/` — 任务书（冻结契约）与验收结论
- `logs/` — 工作日志（每 Agent 每天一个文件）
- `inbox/` — 跨会话消息的降级通道（无消息工具时用）

## 消息格式（统一头，跨会话传递）
每条消息正文用固定块：
【消息类型】task-card | dev-ready | accept-result | fix-done | question | accepted
【任务号】T<YYYYMMDD>-<seq>
【版本】整数；字段/契约变更时 +1
【发送方】<agent-id>@<会话ID>
【接收方】<agent-id>@<会话ID>
【时间】YYYY-MM-DDTHH:MM:SS+08:00
【正文】...

## 自动协作流程
0. **planner 先向用户提问澄清需求，直到用户确认理解**（未确认不写任务书、不派发）
1. planner 写任务书（含冻结契约）→ 发【task-card】
2. developer 读任务书 → 实现 → 发【dev-ready】（附变更清单 + 测试结果）
3. planner 对照契约验收 → 发【accept-result】`accepted` / `rejected`+问题清单
4. `rejected` → developer 修复 → 发【fix-done】→ 回到 3（循环直到 `accepted`）
5. `accepted` → 任务归档，结束

## 可追溯性（责任可查）
- **身份**：每个 Agent 启动即在 `agent-registry.md` 注册 agent-id / 职责 / 会话 ID
- **消息**：每条带发送方 / 接收方 / 任务号 / 版本 / 时间
- **日志**：`logs/<agent-id>-<日期>.md`，含文件清单、结论、测试结果
- **产出物**：任务书 `tasks/` + 代码变更清单，全部落盘可查

## 用 Codex 跨会话消息（首选）
- `send_message_to_thread`：给对端会话发消息
- `handoff_thread`：把当前任务交接给对端
- `read_thread` / `wait_threads`：读取对端回复
- `set_thread_title`：会话标题带 agent-id，便于识别归属
- 工具不可用时降级：写 `inbox/<task-id>-<from>.md`，对端轮询读取