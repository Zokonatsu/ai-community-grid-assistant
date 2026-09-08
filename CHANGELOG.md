# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 安全加固（生产上线问题清单）

- **镜像可构建**：`deploy/Dockerfile` 基础镜像由不存在的 `python:3.14-slim` 修正为 `python:3.12-slim`。
- **CORS 收紧**：移除 `allow_origins=["*"]`，改为 `CORS_ALLOW_ORIGINS` 环境变量白名单
  （默认含本机与生产前端 `http://118.31.58.191:8000`）；白名单含 `*` 时自动降级
  `allow_credentials=False`。
- **事件/任务数据字段级加密**：复用 `DATA_ENCRYPTION_KEY`（AES-256-GCM）对
  `tasks.json` 13 个敏感字段、`events.jsonl` 6 个敏感字段做 `enc:v1:` 加密；
  存量明文兼容读取，提供一次性迁移脚本 `scripts/migrate_events_encryption.py`。
- **限流与熔断**：接入 `slowapi`（登录/注册 5 次/分钟/IP、事件提交 10 次/分钟/用户，
  统一 429 文案）与 `tenacity` 指数退避重试 + 熔断器（连续失败 5 次熔断 60s，
  熔断期间 LLM 调用降级为「待审核」）；参数均可通过环境变量覆盖。
- **监控与告警**：接入 Prometheus 指标端点 `GET /metrics`（`prometheus-fastapi-instrumentator`），
  告警规则见 `docs/监控告警.md`。
- **日志 PII 脱敏**：新增 `log_redact.py`，日志中的手机号/身份证号按
  `LOG_REDACT`（默认开启）掩码输出，覆盖事件描述/地址相关全部日志点。
- **Nginx 反向代理配置**：新增 `deploy/nginx.conf`（单实例反代 + 多副本 upstream
  注释段 + `/metrics` 内网限制 + HTTPS 示例），与 `deploy/DEPLOY.md` §4/§8.4 配套。

### 文档

- `deploy/DEPLOY.md`：新增「回滚 SOP」（§7）、「扩容与多实例」（§8）、
  「监控与告警」（§9），§4 改为引用真实 `nginx.conf`。
- 新增 `docs/监控告警.md`（Prometheus 采集配置 + 4 条告警规则 + 指标安全建议）。
- 新增 `scripts/loadtest/`：k6 压测脚本与使用说明（问题 15）。

### 测试

- `tests/test_auth.py`：修正 7.3/7.4 与代码事实不符的旧文案，改为硬断言
  （logout 200 → 同一 token `/api/auth/me` 401，无/无效 token 幂等 200）。
- 新增 `tests/test_cors.py`、`tests/test_field_encryption.py`、
  `tests/test_rate_limit_circuit.py`、`tests/test_metrics.py`、
  `tests/test_log_redact.py`。
- CI（`.github/workflows/ci.yml`）：新增 Docker 镜像构建 + Trivy 漏洞扫描步骤。## [未发布]

### 新增：系统公告模块
- **后台管理**（仅超管）：新增「系统公告」卡，可对公告进行新增/编辑/查看/删除。单条公告包含标题、富文本正文、生效时间、到期时间。
- **居民强制阅读**：居民登录后自动检测「已生效、未过期、未阅读」公告，弹窗无关闭叉、底部仅「已阅读」；多条按发布时间从早到晚逐个弹出，全部读完才放行进入事件列表；已读记录写入 SQLite，后续登录不再叵台。
- **富文本编辑：**`contenteditable` + 简单工具条（加粗/无序列表/标题），正文以 `innerHTML` 保存/渲染。

### 新增：事件列表分页
- **后台管理页 + 用户端事件列表**均新增分页，每页最多 20 条；支持「上一页/下一页 + 页码」，当前页高亮，并显示「共 X 条 · 第 Y/Z 页」。
- **页码保持：**后台 10 秒轮询刷新、用户端 15 秒轮询刷新保持当前页；手动刷新/点「筛选」回到第 1 页。

### 新增：SLA 与周报指标
- **平均响应时长**（创建→首次处理）、**平均处理时长**（首次处理→完成），均按「上班时段有效时长」切片，自动剔除非工作时间/周末。
- **办结率** = 已完成 / 本周创建；新增 **SLA 响应达标率**；指标颜色 ≤5 绿 / 5–20 黄 / >20 红。
- **数据清洗：**仅已办结工单参与计算；剔除废弃工单（已拒绝/已撤销）与时间脏数据；工单/周报与后台 SLA 口径一致（复用上班时段切片函数）。
- **部门指标表：**一行一部门，显示工单数、平均响应/处理；无工单显示「无工单」。
- **风险区：**分重复工单/超时工单/积压工单三类，汇总前置、明细默认折叠，优先展示高频重复用户。
- **周比：**与上周平均响应/处理、办结率对比并说明涨跌。

### 新增：AI 周报
- **7 段固定结构（物业内部简报风格）：**一句话概况 / 工单类型分布 / 本周亮点 / 需留意的问题 / 上周对比 / SLA 响应达标情况 / 下周工作建议，语言亲切自然、不用代码变量名、段落简短。
- **弹窗功能：**顶部横向统计卡片、类型标签、部门指标表、风险区、底部「复制周报文本/导出 Markdown」；支持自定义起止日期、历史周回看；指标悬浮 Tooltip 说明「仅统计上班时段耗时」。
- **生成等待信号：**点「生成/刷新」戶转圈、完成后渲染周报。

### 新增：存储层（SQLite）
- 新增 `db.py`：使用标准库 `sqlite3` 建表 `announcement_table` / `user_announcement_read` / `report_table`，并开启 WAL；引入 `threading.Lock` 保护并发录写。
- **周报迁移：**启动时将旧 `data/weekly_reports.json` 自动迁入 `report_table`，之后周报读写走 SQLite。

### 变更
- **AI 周报弹窗：**去掉「全部部门」下拉与「本周」快捷按钮；打开弹窗默认本周，后端默认全部部门。
- **弹窗滚动隔离：**回复/详情/AI 周报/编辑部门账号/图片放大均实现「仅在弹窗内滚动」（`body.modal-lock` + `touch-action: pan-y` + `overscroll-behavior: contain`）。

### 修复
- 「本周」按钮日期用 `toISOString()` 导致 UTC 偏移/前一天 → 改为本地时区并自动生成本周报告。
- 生成周报无等待反馈 → 新增转圈加载。
- 部门筛选此前仅过滤指标而非创建数 → 全量按部门过滤。
- 无工单部门在周报表格显示「无工单」。

### 技术栈 / 说明
- 新增依赖：无（仅用 Python 标准库 `sqlite3`；周报 AI 复用现有的 DeepSeek/OpenAI 客户端）。
- 数据层为「加密文件 + SQLite」混合：用户/会话/任务仍为机密文件，公告/已读/周报在 SQLite（`data/app.db`）。
- 公告正文由超管维护，以 `innerHTML` 渲染。

