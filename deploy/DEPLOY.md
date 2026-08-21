# 部署手册

本文档面向运维人员，指导如何在云服务器上完成 AI 社区网格员助手 的 Docker 部署与日常维护。

---

## 1. 服务器选购建议

| 配置项 | 建议 | 说明 |
|:---|:---|:---|
| 云厂商 | 阿里云 / 腾讯云 | 轻量应用服务器即可，性价比较高 |
| CPU | 2 核 | 足够支撑单服务容器运行 |
| 内存 | 2 GB | 满足 Python + FastAPI + 并发请求需求 |
| 带宽 | 3 Mbps 及以上 | 前端页面与 API 响应流畅 |
| 系统盘 | 40 GB SSD | 预留 Docker 镜像与数据文件空间 |
| 操作系统 | Ubuntu 22.04 LTS | 官方长期支持，Docker 兼容性最佳 |

> 生产环境如需高可用，建议搭配负载均衡并横向扩展（约束与方案见第 8 章「扩容与多实例」）。

---

## 2. 服务器环境准备

### 2.1 安装 Docker

```bash
# 更新软件源
sudo apt-get update

# 安装必要依赖
sudo apt-get install -y ca-certificates curl gnupg lsb-release

# 添加 Docker 官方 GPG 密钥
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# 添加 Docker 软件源
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# 安装 Docker Engine
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 验证安装
sudo docker --version
sudo docker compose version
```

### 2.2 配置 Docker 非 root 访问（可选但推荐）

```bash
sudo usermod -aG docker $USER
newgrp docker
```

---

## 3. 项目部署步骤

### 3.1 克隆代码

```bash
cd /opt
git clone https://github.com/Zokonatsu/ai-community-grid-assistant.git
cd ai-community-grid-assistant
```

### 3.2 配置环境变量

```bash
cp .env.example .env
vim .env
```

`.env` 文件内容示例：

```env
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# 账号数据加密密钥（必填，缺失则服务拒绝启动）
DATA_ENCRYPTION_KEY=<64 位十六进制>

# CORS 跨域来源白名单（可选；逗号分隔，默认本机 + 生产前端 http://118.31.58.191:8000）
# CORS_ALLOW_ORIGINS=http://127.0.0.1:8000,http://localhost:8000,http://118.31.58.191:8000

# 限流（可选，默认开启）：登录/注册 5 次/分钟/IP，POST /api/events 10 次/分钟/用户
# 超限统一返回 HTTP 429 + {"detail": "请求过于频繁，请稍后再试"}
# RATE_LIMIT_ENABLED=true
# RATE_LIMIT_LOGIN=5/minute
# RATE_LIMIT_EVENTS=10/minute

# LLM 调用可靠性（可选）：瞬时失败退避重试 2 次（1s/2s）+ 连续失败 5 次熔断 60s
# LLM_RETRY_ATTEMPTS=2
# LLM_RETRY_BASE_DELAY=1.0
# LLM_CIRCUIT_THRESHOLD=5
# LLM_CIRCUIT_COOLDOWN=60
```

> **注意**：`LLM_API_KEY` 必须替换为真实密钥。`.env` 文件已加入 `.gitignore`，不会上传至代码仓库。

### 3.2.1 生成账号加密密钥

`DATA_ENCRYPTION_KEY` 用于加密 `secure/` 目录下的账号/会话数据（AES-256-GCM）。生成方式：

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

将输出复制到 `.env` 的 `DATA_ENCRYPTION_KEY`。

> **⚠️ 密钥必须单独备份**（与服务器上的加密数据分开存放）。密钥 + 加密文件两者缺一不可：密钥丢失 = 全部账号数据永久无法解密，无法找回。

### 3.3 启动服务

```bash
sudo docker compose up -d
```

### 3.3.1 限流与 429 说明

生产默认开启单机内存限流（slowapi）：
- 登录/注册：按客户端 IP **5 次/分钟**（`RATE_LIMIT_LOGIN`）；
- 事件提交 `POST /api/events`：按登录用户 `user_id` **10 次/分钟**（`RATE_LIMIT_EVENTS`，无 token 按 IP）。

超限统一返回 **HTTP 429** + `{"detail": "请求过于频繁，请稍后再试"}`，不计入业务失败、不落库。
如需临时放量（压测/活动）可设 `RATE_LIMIT_ENABLED=false` 重启；阈值按需调大两个 `RATE_LIMIT_*` 变量。
LLM 调用侧另有退避重试（`LLM_RETRY_ATTEMPTS`/`LLM_RETRY_BASE_DELAY`）与熔断
（`LLM_CIRCUIT_THRESHOLD`/`LLM_CIRCUIT_COOLDOWN`），上游 API 长时间故障时服务降级不崩，
相关状态与计数在 `receive_agent` 日志中可见。


```bash
# 查看容器运行状态
docker compose ps

# 测试健康检查端点
curl http://127.0.0.1:8000/health
```

预期返回：

```json
{"status":"ok"}
```

浏览器访问 `http://<服务器公网IP>:8000/` 即可进入前端管理页面。

### 3.5 云存储模式（账号数据上云，可选）

默认模式下账号数据保存在服务器的本地加密文件 `secure/users.json.enc`。若希望账号数据存在**腾讯云对象存储**（服务器重装/重建也不丢账号），可开启云存储模式。

> **实现说明**：腾讯云 CloudBase 云存储底层即为对象存储（COS）。本项目使用腾讯云官方 **COS Python SDK**（`cos-python-sdk-v5`）读写，无数据库依赖。

**数据放哪：**

| 数据 | 云存储模式 | 说明 |
|:---|:---|:---|
| 账号（含密码哈希） | ☁️ 云端对象 `users.json.enc` | AES-256-GCM 加密后上传，云端只见密文 |
| 会话（session） | ☁️ 云端对象 `sessions.json.enc` | AES-256-GCM 加密后上传，登出即删 |
| 事件/任务 | 💻 本地 `data/` | 明文事件数据，不上云 |

**配置步骤：**

1. 登录腾讯云控制台，在「对象存储 COS」创建存储桶，**权限建议私有读写**。
2. 在「访问管理 CAM」创建**仅限该存储桶读写**的子账号，记录其 API 密钥 `SecretId` / `SecretKey`。
3. 在服务器 `.env` 增加（密钥只放服务器，切勿提交/外泄）：

   ```env
   AUTH_STORE=cloudbase
   COS_REGION=ap-guangzhou          # 存储桶所在地域
   COS_BUCKET=your-bucket-name      # 存储桶名称
   COS_SECRET_ID=<SecretId>
   COS_SECRET_KEY=<SecretKey>
   ```

   `AUTH_STORE` 缺省为 `file`（本地模式）；`AUTH_STORE=cloudbase` 时缺任一 `COS_*` 服务将拒绝启动。

> **本地开发/测试**：如需本地模式，临时设置环境变量 `AUTH_STORE=file`（Windows PowerShell：`$env:AUTH_STORE='file'`）或使用独立 `.env` 启动即可，无需连云。

4. **首次启用前**，把本地已有账号迁移到云端（仅需一次）：

   ```bash
   # 服务器上执行；要求 .env 已配置 COS_* 且本地存在 secure/users.json.enc
   docker compose exec app python scripts/init_cloud_storage.py --yes
   ```

   - 脚本把加密 blob **字节原样**上传，本地文件保留（回滚备份）；
   - 上传完成后重启服务，账号即读写云端。

5. 重启生效：

   ```bash
   docker compose up -d --build
   ```

**回滚到本地模式：**

```bash
# .env 中改回 AUTH_STORE=file，重启即可
docker compose restart
```

> 回滚后服务读取本地 `secure/users.json.enc`（迁移时已保留）。若本地文件已过期，可在回滚前先切回 `cloudbase` 拉取最新数据再上传覆盖本地。

**云存储模式注意事项：**

- **备份**：账号权威数据在云端对象 `users.json.enc`。仍建议定期下载一份 `secure/users.json.enc`（连同 `DATA_ENCRYPTION_KEY`）作为异地备份。
- **并发写**：对象存储为"后写覆盖"（last-writer-wins）。当前单实例由进程锁串行写入，安全；若将来部署多实例，需引入分布式锁或改按用户分对象存储（文档待补）。
- **密钥轮换**：云上 blob 同样用 `DATA_ENCRYPTION_KEY` 加密，轮换密钥需先下载→重加密→重新上传。

---

## 4. Nginx 反向代理配置（可选）

仓库提供完整可用的配置文件 **`deploy/nginx.conf`**（单实例反向代理 + 多副本
upstream 注释段 + `/metrics` 内网限制 + HTTPS 示例），部署时直接复制启用即可，
无需手抄配置：

```bash
# 安装 nginx（如未安装）
sudo apt-get install -y nginx

# 复制并启用站点配置
sudo cp deploy/nginx.conf /etc/nginx/sites-available/grid-assistant
sudo ln -s /etc/nginx/sites-available/grid-assistant /etc/nginx/sites-enabled/

# 若默认站点存在且 server_name 冲突，先禁用：
# sudo rm /etc/nginx/sites-enabled/default

# 按需修改占位符（server_name、upstream 地址、/metrics 放行网段）后测试并重载：
sudo nginx -t
sudo systemctl reload nginx
```

要点：

- **`server_name`**：替换为真实域名（如 `grid.example.com`）；直接通过公网 IP
  访问时填 IP，或写 `_` 匹配任意主机名。
- **`/metrics`**：默认仅放行本机（127.0.0.1/::1）；Prometheus 在其它内网机器时，
  在 `location = /metrics` 中放开对应网段（详见 docs/监控告警.md §5）。
- **限流不受影响**：uvicorn 默认已启用 `--proxy-headers` 并信任 127.0.0.1，
  Nginx 传递的 `X-Forwarded-For` 会被解析为真实客户端 IP，登录/事件限流按
  真实 IP 计数。
- **健康检查**：`location = /health` 已放行，返回 `{"status":"ok"}`。

如需 HTTPS，建议使用 [Certbot](https://certbot.eff.org/) 自动申请并配置
Let's Encrypt 证书（`deploy/nginx.conf` 文末附有手工 443 配置参考）：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d grid.example.com
```

## 5. 服务维护命令

### 5.1 查看日志

```bash
# 实时跟踪日志
docker compose logs -f

# 查看最近 100 行
docker compose logs --tail=100
```

### 5.2 重启服务

```bash
# 重启容器
docker compose restart

# 强制重建并重启（修改代码后）
docker compose up -d --build
```

### 5.3 更新版本

```bash
# 拉取最新代码
git pull origin main

# 重新构建并启动
docker compose up -d --build
```

### 5.4 停止服务

```bash
docker compose down
```

### 5.5 备份数据

```bash
# 事件/任务等明文数据
cp ./data/events.jsonl ./data/events.jsonl.bak.$(date +%Y%m%d)

# 加密后的账号/会话数据（连同密钥一起备份）
cp -r ./secure ./secure.bak.$(date +%Y%m%d)
```

> **注意**：`secure/` 下的文件已加密，需与 `.env` 中的 `DATA_ENCRYPTION_KEY` 一起备份才可用。
>
> **云存储模式**（`AUTH_STORE=cloudbase`）：账号权威数据在云端，此处的 `secure/users.json.enc` 为本地回滚备份，仍建议定期下载到服务器外保存。

---

## 6. 安全注意事项

### 6.1 防火墙

仅开放必要端口，建议配置如下：

| 端口 | 用途 | 建议 |
|:---|:---|:---|
| 22 | SSH | 限制指定 IP 访问，或改用密钥登录 |
| 80 | HTTP | 如使用 Nginx 反向代理，则对外开放 |
| 443 | HTTPS | 如使用 Nginx + SSL，则对外开放 |
| 8000 | FastAPI 服务 | **不建议直接对外开放**，应通过 Nginx 反向代理访问 |

阿里云 / 腾讯云安全组配置示例：

```bash
# 仅允许本机访问 8000 端口（通过 Nginx 反向代理）
# 安全组规则：入方向，来源 IP 填写 127.0.0.1/32，端口 8000
```

### 6.2 密钥管理

- `.env` 文件权限应设为 `600`，禁止其他用户读取：
  ```bash
  chmod 600 .env
  ```
- 定期轮换 `LLM_API_KEY`。
- 禁止将密钥硬编码在代码中或提交至 Git 仓库。
- `DATA_ENCRYPTION_KEY` 需单独备份（与加密数据分开存放），并限制读取权限：
  ```bash
  chmod 700 secure          # 目录仅所有者可进入
  chmod 600 secure/*.enc    # 加密文件仅所有者可读写
  ```
- 如需轮换 `DATA_ENCRYPTION_KEY`（重加密现有数据），在服务器上执行：
  ```bash
  python3 secure_store.py genkey        # 生成新密钥
  DATA_ENCRYPTION_KEY=<旧密钥> python3 secure_store.py rekey --new <新密钥>
  # 然后把 .env 中 DATA_ENCRYPTION_KEY 更新为新密钥并重启服务
  ```

### 6.3 首次升级迁移说明

旧版本账号数据以明文存放在 `data/users.json`、`data/sessions.json`。升级到本版本后，首次启动会自动将其加密迁移到 `secure/`，原文件改名为 `*.migrated.bak` 保留现场。

- **迁移是幂等的**：`secure/` 一旦存在加密文件即为权威，明文数据不再被读取。
- 迁移后请确认原账号仍可登录，再手动清理 `data/users.json.migrated.bak`。
- **切勿**删除 `secure/` 下的加密文件或丢失密钥，否则账号数据无法恢复。

### 6.4 容器安全

- 容器以非 root 用户运行（已在 Dockerfile 中配置）。
- 定期更新基础镜像：
  ```bash
  docker compose pull
  docker compose up -d
  ```

## 7. 标准化回滚 SOP

> 适用场景：按 `deploy/Dockerfile` 从源码构建镜像 + `docker compose` 部署的常规发布/升级失败回滚。
> 流程：发布前检查清单（7.1）→ 回滚步骤（7.2）→ 回滚决策与验证标准（7.3）→ 注意事项（7.4）。

### 7.1 发布前检查清单

每次发布/升级前，运维必须逐项确认（建议在发布窗口前完成）：

| # | 检查项 | 操作命令 | 目的 |
|:--|:--|:--|:--|
| 1 | 备份事件/任务明文数据 `data/` | `cp -r ./data ./data.bak.$(date +%Y%m%d_%H%M%S)` | 回滚时可整体恢复到发布前时间点（含 events.jsonl / tasks.json / community_config.json） |
| 2 | 备份加密账号 `secure/` | `cp -r ./secure ./secure.bak.$(date +%Y%m%d_%H%M%S)` | 与密钥成对恢复，保证账号数据可解密 |
| 3 | 备份 `.env` 与加密密钥 | `cp .env .env.bak.$(date +%Y%m%d_%H%M%S)`；`DATA_ENCRYPTION_KEY` 与加密文件**分开**另行安全存放 | 密钥丢失即账号数据永久无法解密 |
| 4 | 记录当前镜像标识 | `docker compose images`（记录当前 IMAGE ID/TAG）；本地保留上一版本镜像（`docker images`） | 回滚时能准确定位「上一版本镜像」 |
| 5 | 核对 `CORS_ALLOW_ORIGINS` 白名单 | `grep CORS_ALLOW_ORIGINS .env`（确认含生产前端域名，且不含 `*`） | 防止 CORS 全开或漏配生产前端域名导致跨域失效 |

> **云存储模式**（`AUTH_STORE=cloudbase`）：账号权威数据在腾讯云 COS，仍需备份本地 `data/` 与 `.env`；`secure/` 本地副本作为回滚兜底一并备份（见 5.5）。

### 7.2 回滚步骤

```bash
# 1) 停止当前容器（数据在宿主机 data/ 与 secure/ 卷，不删除）
docker compose down

# 2) 恢复备份（<TS> 替换为 7.1 实际时间戳；先移走新版本产生的数据留证）
mv ./data          ./data.failed_<TS>
mv ./data.bak.<TS> ./data
mv ./secure        ./secure.failed_<TS>
mv ./secure.bak.<TS> ./secure
mv ./.env.bak.<TS> ./.env

# 3) 用上一版本镜像启动
#    方式 A（推荐，镜像仍在本地）：把上一版本镜像重新 tag 后拉起
docker tag <上一版本IMAGE ID或TAG> ai-community-grid-assistant-app:latest
docker compose up -d
#    方式 B（镜像不在本地）：回到上一版本代码重新构建
git checkout <上一版本tag或commit>
docker compose up -d --build

# 4) 健康检查（预期 {"status":"ok"}）
curl http://127.0.0.1:8000/health

# 5) 抽查数据可解密
#    - 服务能正常启动即证明 secure/*.enc 与 .env 的 DATA_ENCRYPTION_KEY 匹配
#      （密钥不匹配时服务 fail-fast 拒绝启动，见 6.2 / README「账号数据加密存储」）
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"<测试账号>","password":"<密码>"}'
#    预期 success=true；失败则说明账号数据/密钥恢复不正确
```

执行完上述步骤后，用 7.3 的**验证命令**与标准逐项确认回滚成功。

### 7.3 回滚决策与验证标准

**出现以下任一情况即触发回滚：**

- 容器启动失败或崩溃循环（`docker compose ps` 显示 restarting/Exited）。
- `GET /health` 非 200 或返回体非 `{"status":"ok"}`。
- 核心 API 异常：`POST /api/events` 持续失败、任务列表/详情接口报错、前端页面不可用。
- 账号无法登录或解密报错（密钥不匹配、`secure/*.enc` 损坏或与代码版本不兼容）。
- 日志出现业务性 ERROR 且无法在短时间内修复（`docker compose logs --tail=200`）。

**回滚成功的验证标准（全部满足）：**

1. `docker compose ps`：服务容器 running/healthy。
2. `curl http://127.0.0.1:8000/health` 返回 `{"status":"ok"}`。
3. 事件列表接口返回数据，且与 7.1 备份一致（抽查最近 N 条时间戳与内容）。
4. 抽查数据可解密：7.2 步骤 5 登录接口返回 `success=true`。
5. `docker compose logs --tail=100` 无新增业务性 ERROR。

### 7.4 注意事项

- **events.jsonl 追加不回滚**：`data/events.jsonl` 为追加式写入，回滚**不会**自动移除回滚前新版本写入的记录；若需精确回到发布前时间点，必须用 7.1 备份的 `events.jsonl` **整体覆盖**当前文件，切勿手工合并。
- **tasks.json 以备份为准**：`data/tasks.json` 是整文件覆盖写（进程内 `_tasks` 全量落盘），回滚时以备份文件整体恢复为准，不要手工拼接/合并，否则会造成任务状态不一致。
- **密钥与加密文件必须成对恢复**：`secure/*.enc` 与 `.env` 中的 `DATA_ENCRYPTION_KEY` 必须来自**同一次备份快照**；只恢复其中一个会导致服务启动失败（fail-fast）或账号永久无法解密。
- **云存储模式**：`AUTH_STORE=cloudbase` 时账号权威在云端，回滚主要恢复本地 `data/` 与 `.env`；若同时需要账号回滚到旧版本，需处理 COS 中 `users.json.enc` / `sessions.json.enc`（用备份覆盖或启用对象版本管理），避免新旧版本加密数据混用。
- 回滚后保留 `*.failed_<TS>` 目录留证，确认稳定后再手动清理。

---

## 8. 扩容与多实例

### 8.1 单实例约束说明（当前架构前提）

当前实现以「单容器单进程」为设计前提，以下状态与机制**仅保证单实例内一致**，直接多副本会出问题：

| 约束 | 说明 |
|:--|:--|
| 内存 `_tasks` | `main.py` 在进程内维护全量任务字典 `_tasks`（启动时从 `data/tasks.json` 加载，运行中整份写回）；不同实例的内存副本彼此不可见 |
| 本地写锁 | `record_agent` 的 events.jsonl 追加锁、`main` 的 `_task_lock`、`auth` 的 `_auth_lock` 均为**进程内锁**，只保护单进程内的并发，不跨进程 |
| `secure/` 本地加密文件 | `AUTH_STORE=file` 时账号/会话存本地 `secure/*.enc`，多实例各自读写会互相覆盖/不一致 |
| `events.jsonl` 追加并发 | 多进程同时以追加方式写同一文件**没有跨进程锁**，行记录可能交错甚至丢失 |

因此「多副本 + 各副本直接读写同一本地目录」并不安全，扩容请按下述方案执行。

### 8.2 扩容方案 A：单机升配（推荐首选，改动最小）

单实例资源不足（CPU/内存/带宽）时，先升级单机配置：

1. 云控制台升配 CPU/内存/带宽/系统盘（多数厂商支持热升级或需重启一次）。
2. 事件/任务数据仍在本地 `data/`，无需迁移。
3. 账号数据建议同步上云，消除本地 `secure/` 单点：
   - `.env` 设置 `AUTH_STORE=cloudbase` 并配置 `COS_*`（见 3.5 云存储模式）。
   - 上云后账号权威在 COS，实例重装/重建不丢账号。

**适用**：流量增长有限、单机可支撑；无需改代码/架构，运维成本最低。

### 8.3 扩容方案 B：多副本（需先解决数据层一致性）

横向扩展多副本前，必须先解决数据一致性问题，二选一：

1. **共享数据卷**：把 `data/`（events.jsonl / tasks.json / community_config.json）与 `secure/` 放到共享存储（NFS、Ceph、云盘多挂载），所有副本挂载同一目录；账号建议同时 `AUTH_STORE=cloudbase` 上云。
2. **对象存储/数据库**：把事件与任务持久化迁移到对象存储或数据库（当前为本地文件，需代码改造，超出本文档范围）。

**必须明确的并发风险与建议：**

- **events.jsonl 多进程追加并发风险**：多进程同时追加写同一 `events.jsonl` 无跨进程锁，行可能交错或丢失。建议：① 单写者——仅一个副本负责写 `events.jsonl`，其余副本只读；② 按实例分片文件（如 `events-<实例ID>.jsonl`）再统一采集合并；③ 迁移到数据库/消息队列后统一落库。
- **tasks.json 整文件覆盖写**：多实例同时写 `data/tasks.json` 会互相覆盖（last-write-wins），任务状态可能丢失。同样建议单写者或迁移数据库。
- **secure/ 本地加密文件**：多副本各自读写会不一致，必须 `AUTH_STORE=cloudbase` 上云，或共享卷 + 单写者。
- **负载均衡会话保持**：会话 Token 存 `secure/`（本地模式下为实例本地文件），多副本需共享存储/上云，或负载均衡开启会话保持（sticky session），否则登录态会随机失效。

### 8.4 Nginx 多 upstream 示例（可选）

多副本就绪后，Nginx 可配置多个 upstream（完整可用的配置文件见 `deploy/nginx.conf` 中 `upstream grid_app` 的注释段，以下为示意图）：

```nginx
upstream grid_app {
    server 10.0.0.11:8000;   # 实例 1
    server 10.0.0.12:8000;   # 实例 2
    # 本地会话模式建议开启会话保持（多副本共享数据层/账号上云后可去掉）
    ip_hash;
}

server {
    listen 80;
    server_name grid.example.com;

    location / {
        proxy_pass http://grid_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

配置完成后测试并重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

> **建议**：多副本（方案 B）仅在事件/任务写入并发可控（已采用单写者或完成数据层改造）时使用；否则优先方案 A。


---


## 9. 监控与告警（Prometheus）

本服务已接入 Prometheus 指标端点 `GET /metrics`（无鉴权、不参与业务限流、
不进 OpenAPI），指标清单、Prometheus 采集配置、Alertmanager 告警规则示例
与安全建议详见 **docs/监控告警.md**。

```bash
# 验证指标端点（本机）
curl -s http://127.0.0.1:8000/metrics | head -20
```

要点速览：

- **采集**：在 Prometheus `scrape_configs` 增加 `job_name: grid-assistant`，
  target 为 `127.0.0.1:8000`（或 docker-compose 网络内 `app:8000`），
  `metrics_path: /metrics`，`scrape_interval` 建议 15s；
- **告警**：Alertmanager 规则示例（5xx 比例 / 接口 P95 耗时 / 实例存活 /
  事件处理失败率预留）见 docs/监控告警.md 第 4 节；
- **安全**：`/metrics` 无鉴权，生产建议经 Nginx 反向代理限制仅内网/运维网段
  可访问，业务端口 8000 不直接开放公网（见本章 6.1 防火墙）。


## 附录：常用诊断命令

```bash
# 查看容器资源占用
docker stats

# 进入容器内部排查
docker compose exec app /bin/sh

# 查看容器内进程
docker compose exec app ps aux

# 测试 API 接口
curl -X POST http://127.0.0.1:8000/api/events \
  -H "Content-Type: application/json" \
  -d '{"description":"测试部署是否成功"}'
```


---

## 上线前回归测试（core / full）

发布前建议先跑本地回归（无需真实密钥，conftest 自动固定测试环境并隔离 data/secure）：

```bash
python tests/run_regression.py core      # P0 核心集（CI 必跑）
python tests/run_regression.py full      # 全量 16 个脚本（test_server 远程冒烟默认 skip）
```

失败时退出码非 0；汇总报告含逐脚本通过/失败、耗时与「残留备份检查=无」。
详见 README「回归测试」一节。
