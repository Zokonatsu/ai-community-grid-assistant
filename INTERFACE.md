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

## 规则
- 接收Agent只负责提取信息，不决定派给谁
- 派发Agent只负责匹配处理方，不修改其他字段
- 两个Agent通过State共享数据
- HTTP API 契约改动时，本文件与前端 `static/`、测试 `tests/` 需同步更新