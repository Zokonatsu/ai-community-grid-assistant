# Agent 身份注册表

> 规则：Agent 启动时追加一行（登记职责与会话 ID），任务切换时更新「当前任务」列。下行为示例，请替换为实际值。

| agent-id | 角色 | 会话 ID | 启动时间 | 当前任务 | 状态 |
|---|---|---|---|---|---|
| planner-01 | 产品规划 | planner@01a01a02-fd78-70e2-b6fc-97a86b7d684c | 2026-08-19T19:05:41+08:00 | T20260820-002 | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-19T19:12:34+08:00 | T20260819-001 | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-19T20:00:00+08:00 | T20260819-002 | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-19T21:48:00+08:00 | T20260819-003 | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-19T22:50:00+08:00 | T20260819-004 | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-19T23:59:40+08:00 | T20260819-005 | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-20T00:35:00+08:00 | T20260820-001-TA | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-20T02:20:00+08:00 | T20260820-001-TC | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-20T01:20:00+08:00 | T20260820-001-TB | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-20T02:35:00+08:00 | T20260820-001-TD | 完成（已归档） |
| developer-01 | 开发 | developer@developer-01 | 2026-08-20T03:00:00+08:00 | T20260820-002 | 完成（已归档） |
| planner-01 | 产品规划 | planner@01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T14:46:01+08:00 | T20260821-001~008（生产上线问题清单） | 全部归档（含事故记录） |
| developer-01 | 开发 | developer-sub-T001@planner-01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T15:20:00+08:00 | T20260821-001 | 完成（已归档） |
| developer-02 | 开发 | developer-sub-T002@planner-01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T15:20:00+08:00 | T20260821-002 | 完成（已归档） |
| developer-03 | 开发 | developer-sub-T003@planner-01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T15:20:00+08:00 | T20260821-003 | 完成（已归档） |
| developer-04 | 开发 | developer-sub-T004@planner-01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T15:20:00+08:00 | T20260821-004 | 完成（已归档） |
| developer-05 | 开发 | developer-sub-T005@planner-01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T15:20:00+08:00 | T20260821-005 | 完成（已归档） |
| developer-06 | 开发 | developer-sub-T006@planner-01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T15:20:00+08:00 | T20260821-006 | 完成（已归档） |
| developer-07 | 开发 | developer-sub-T007@planner-01a0230e-cc29-7490-bddb-9d1ae703876a | 2026-08-21T15:20:00+08:00 | T20260821-007 | 完成（已归档） |
