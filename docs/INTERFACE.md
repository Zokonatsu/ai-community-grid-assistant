# 模块接口约定

## 接收Agent (receive_agent.py)
输入: {"description": "居民原始描述"}
输出: {
    "address": "具体地址",
    "event_type": "物业维修/环境卫生/安全隐患/邻里纠纷/公共设施/其他",
    "urgency": "高/中/低",
    "handler": ""  // 初始为空，由派发Agent填充
}

## 派发Agent (dispatch_agent.py)
输入: 接收Agent的完整输出
输出: {
    "handler": "物业/城管/消防/调解员/社区"
}

## HTTP API（create_event）

`POST /api/events`（需登录 Bearer token），请求体字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `description` | str | 是 | 居民原始描述 |
| `beneficiary_type` | str | 否 | `self`（本人）/ `proxy`（代人办），默认 `self` |
| `beneficiary_name` | str | proxy 时是 | 被帮助人姓名 |
| `beneficiary_phone` | str | proxy 时是 | 被帮助人手机号 |
| `beneficiary_building` | str | proxy 时是 | 被帮助人楼栋 |
| `beneficiary_unit` | str | proxy 时是 | 被帮助人单元 |
| `beneficiary_room` | str | proxy 时是 | 被帮助人房间号 |
| `lat` / `lng` | float | 否 | 定位坐标（未定位可为 null） |
| `confirmed` | bool | 否 | 高风险二次确认，默认 `false` |
| `emergency_type` | str | 否 | 急救场景 `medical/police/fire`（仅 confirmed 时） |

校验规则：
- `beneficiary_type` 非 `self`/`proxy` → 400「提交方式不合法」
- `proxy` 时缺被帮助人必填项 → 400
- `lat` 范围 `[-90,90]`、`lng` 范围 `[-180,180]`，越界 → 400


## 限流（429）

服务内置单机内存限流（slowapi，`RATE_LIMIT_ENABLED` 控制，默认开启）：
- `POST /api/auth/login`、`POST /api/auth/register`：keyfunc=客户端 IP，默认 `5/minute`（`RATE_LIMIT_LOGIN`）；
- `POST /api/events`：keyfunc=Bearer token 内 `user_id`，无有效 token 按 IP 兜底，默认 `10/minute`（`RATE_LIMIT_EVENTS`）。

超限统一返回 **HTTP 429**，响应体固定为 `{"detail": "请求过于频繁，请稍后再试"}`
（全局 exception handler，与其它业务错误码/文案独立）。限流计数为单机内存窗口，
服务重启即清零；测试环境建议 `RATE_LIMIT_ENABLED=false` 关闭。

## HTTP API（register）

`POST /api/auth/register`（无需登录），请求体字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | str | 是 | 用户名（3-20 位，中文/字母/数字/下划线） |
| `password` | str | 是 | 密码（至少 6 位） |
| `real_name` | str | 是 | 真实姓名（1-20 字符） |
| `phone` | str | 是 | 手机号（1[3-9] 开头 11 位） |
| `id_card` | str | 否 | 身份证号（可选，非空时校验格式） |
| `role` | str | 否 | `resident`（默认）仅居民；`admin` 一律拒绝 |
| `building` / `unit` / `room` | str | 是（居民） | 楼栋/单元/房间号 |
| `register_lat` / `register_lng` | float|null | 业务必填（schema 可空，避免 422） | 注册定位坐标；缺失/null 时返回业务错误 |

定位强制校验（新注册必过，无关闭开关）：
- 无坐标（`register_lat`/`register_lng` 任一为 `null`）→ `success=false`，error 精确文案：`注册需先获取定位，请允许浏览器定位权限后重试`，不创建用户。
- 越界（距当前生效中心 > `radius_m`）→ `success=false`，error 精确文案：`当前定位不在小区范围内，无法注册`，不创建用户。
- 半径/中心取 `geo.get_community_config()` 当前生效值（`data/community_config.json` 持久化优先，环境变量兜底默认 500m），后台改半径立即生效，无需缓存/重启。
- 范围内（≤ `radius_m`）→ 注册成功，`data.user.location_status` 恒为 `"verified"`；其余响应结构不变（`{"success": true, "data": {"user": {...}}, "error": "注册成功，请登录"}`）。
- 仅对新注册生效；存量用户数据、登录、后台用户列表逻辑不变。

## HTTP API（cancel_event）

`POST /api/events/{event_id}/cancel`（需登录 Bearer token，无请求体）：

| 场景 | 状态码 | 响应 detail（精确匹配） |
|---|---|---|
| 事件不存在 | 404 | `事件不存在` |
| 非本人（含管理员代撤销） | 403 | `无权操作该事件` |
| 状态已是「已撤销」 | 400 | `事件已撤销` |
| 距 created_at 超过 300 秒（或 created_at 解析失败） | 400 | `已超过5分钟，无法撤销` |
| 成功 | 200 | `{"success": true, "data": {"event_id": "<id>", "status": "已撤销"}}` |

说明：
- 仅事件提交者本人可撤销；管理员即使调用也返回 403，不支持代撤销。
- 校验顺序：存在 → 归属 → 已是已撤销 → 5 分钟窗口 → 成功；**不看事件状态**：提交后 5 分钟内任何状态（处理中/待审核/已派单/已完成/已受理/处理超时/处理失败/已拒绝）均可撤销，「已撤销」除外。
- 5 分钟窗口以后端为权威：`now - created_at > 300` 秒即拒绝；`created_at` 按 `"%Y-%m-%d %H:%M:%S"` 解析，解析失败按超时处理。
- 撤销仅将状态标记为「已撤销」，不清除 replies/handler 等既有字段（保留记录，居民与后台仍可见）；不物理删除。
- `GET /api/events` 对已撤销事件照常返回（status=已撤销）。
- `_process_event` 超时/异常分支仅当 status ∈ {处理中, 待审核} 时才改写为处理超时/处理失败，防止覆盖「已撤销」。


## HTTP API（logout）

`POST /api/auth/logout`（可带 Bearer token，无请求体）：

| 场景 | 状态码 | 响应 |
|---|---|---|
| 带有效 token | 200 | `{"message": "登出成功"}` |
| 无 token（幂等） | 200 | `{"message": "登出成功"}` |
| 无效 token（幂等） | 200 | `{"message": "登出成功"}` |

说明：
- 鉴权可选（幂等端点）；带有效 token 时删除服务端 session，使该 token 立即失效。
- 登出后同一 token 调 `GET /api/auth/me` 返回 401「未登录或登录已过期，请重新登录」。
- 无 token / 无效 token 同样返回 200，不抛错。


## HTTP API（metrics）

`GET /metrics`（无需鉴权、不参与业务限流，T20260821-005）：

| 项 | 契约 |
|---|---|
| 状态码 | 200 |
| Content-Type | `text/plain; version=0.0.4; charset=utf-8` |
| 鉴权 | 不需要（无 Authorization 也 200） |
| 限流 | 不参与（slowapi 仅作用于显式 `@limiter.limit` 端点，`RATE_LIMIT_ENABLED` 不影响） |
| OpenAPI | 不展示（`include_in_schema=False`） |
| 必含指标 | `http_requests_total`、`http_request_duration_seconds`（另含 `http_request_duration_highr_seconds`、`http_request_size_bytes`、`http_response_size_bytes` 与进程/Python 运行时指标） |

说明：
- 供 Prometheus 抓取；指标清单、scrape 与 Alertmanager 告警配置示例见 docs/监控告警.md。
- 生产建议经反向代理限制 /metrics 仅内网/运维网段可达。


## 规则
- 接收Agent只负责提取信息，不决定派给谁
- 派发Agent只负责匹配处理方，不修改其他字段
- 两个Agent通过State共享数据
- HTTP API 契约改动时，本文件与前端 `static/`、测试 `tests/` 需同步更新