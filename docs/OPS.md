# 运维手册（OPS）

## 1. 数据备份 / 恢复

### 备份
    python scripts/backup.py                     # 备份到 ./backups，保留最近 7 份
    python scripts/backup.py --out D:/backups    # 指定目录
    python scripts/backup.py --keep 14           # 保留 14 份
    python scripts/backup.py --include-env       # 额外打包 .env（含密钥，务必加密存放）

- 备份内容为 `data/`（tasks.json、events.jsonl）与 `secure/`（AES-256-GCM 加密文件）。
- `backups/` 已在 `.gitignore`，不会进仓库。

### 定时备份（Windows 任务计划程序）
    schtasks /Create /SC DAILY /TN "ai-grid-backup" /ST 02:00 ^
      /TR "C:\Users\78397\Desktop\新\.venv\Scripts\python.exe C:\Users\78397\Desktop\新\scripts\backup.py --out D:\backups"

Linux/cron：
    0 2 * * * cd /path/ai-community-grid-assistant && .venv/bin/python scripts/backup.py --out /var/backups

### 恢复演练（建议每月一次）
    python scripts/restore.py --file backups/backup-<时间戳>.zip --dry-run   # 预览，不落地
    python scripts/restore.py --file backups/backup-<时间戳>.zip --yes      # 实际恢复（覆盖 data/secure）

## 2. 监控与告警

- 应用已暴露 Prometheus 指标：GET /metrics；健康检查：GET /health。
- 计划任务/系统服务周期调用健康探针，失败自动通知：
    python scripts/health_probe.py                # 退出码 0=健康，1=不健康
    设 ALERT_WEBHOOK_URL=<你的通知地址> 可自动推送失败告警。

- Prometheus / Alertmanager 配置模板（接容器或主机监控）：
    deploy/prometheus/prometheus.yml        # 抓取配置
    deploy/prometheus/alert_rules.yml       # 告警规则（可扩展业务计数器）
    deploy/prometheus/alertmanager.yml      # 告警路由（webhook / 邮件，自行填写）

- 接入 Sentry（可选）：设置环境变量 `SENTRY_DSN` 并安装 `sentry-sdk` 后，可在 `main.py` 启用异常上报（见代码内注释）。

## 3. 生产配置提醒（部署前务必处理）
- `ADMIN_INITIAL_PASSWORD` 必须换成强密码。
- `COMMUNITY_REQUIRE_LOCATION` 生产恢复为 `true`。
- 社区半径改为全浙江（或实际需求值）。
- `.env` 不提交；定期用 `scripts/scan_secrets.py` 扫描密钥泄漏。
