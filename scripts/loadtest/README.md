# k6 压测脚本（问题 15）

对服务做并发压力测试，验证「N 个人同时用会不会崩」。

## 安装 k6

```bash
# Ubuntu/Debian
sudo gpg -k https://dl.k6.io/key.gpg
echo "deb https://dl.k6.io/deb stable main" | sudo tee /etc/apt/sources.list.d/k6.list
sudo apt-get update
sudo apt-get install k6

# macOS
brew install k6

# Windows（choco 或直接下载 release 可执行文件）
choco install k6
```

## 运行

```bash
# 只读模式（健康检查，不会触发限流；可用于日常快速冒烟）
k6 run -e BASE_URL=http://127.0.0.1:8000 -e MODE=read scripts/loadtest/k6-loadtest.js

# 混合模式（注册/登录 + 事件提交 + 列表；建议压测前关闭应用限流）
k6 run -e BASE_URL=http://127.0.0.1:8000 -e VUS=50 -e DURATION=2m scripts/loadtest/k6-loadtest.js
```

## 参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `BASE_URL` | `http://127.0.0.1:8000` | 服务地址（压生产可指向公网地址） |
| `VUS` | 50 | 并发虚拟用户数 |
| `DURATION` | `2m` | 稳态压测时长 |
| `RAMP` | `30s` | 爬坡时长（从 0 逐步加到 VUS） |
| `MODE` | `mixed` | `mixed`（读写）/ `read`（只读） |

## 重要：限流与压测

应用自带 slowapi 限流（登录/注册 **5/min/IP**、事件提交 **10/min/用户**）。
混合模式大量注册/登录会得到 429 —— 那是限流在正常工作，不是系统崩溃。

压测目标是「应用容量」，因此混合模式压测前建议：

```bash
# .env 中临时设置，重启服务后压测，压完改回 true
RATE_LIMIT_ENABLED=false
```

只读模式不触发限流，无需关闭。

## 结果解读

k6 结束会输出汇总：

- `http_req_duration` 的 `p(95)`：95% 请求耗时，阈值 < 2000ms
- `http_req_failed`：请求失败率，阈值 < 5%
- 两个阈值任一超标，脚本退出码非 0

建议保留截图：`http_req_duration`（avg/p95/p99）、`http_req_failed`、
`iterations` 与 `vus_max`，作为面试/报告中的压测证据。