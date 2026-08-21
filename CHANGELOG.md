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
- CI（`.github/workflows/ci.yml`）：新增 Docker 镜像构建 + Trivy 漏洞扫描步骤。