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

> 生产环境如需高可用，建议搭配负载均衡并横向扩展。

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

### 3.4 验证部署

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
| 会话（session） | 💻 本地 `secure/sessions.json.enc` | 短期数据，登出即删，无需上云 |
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

4. **首次启用前**，把本地已有账号迁移到云端（仅需一次）：

   ```bash
   # 服务器上执行；要求 .env 已配置 COS_* 且本地存在 secure/users.json.enc
   docker compose exec app python migrate_to_cloud.py
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

如需通过域名访问并启用 HTTPS，参考以下 Nginx 配置：

```nginx
server {
    listen 80;
    server_name grid.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
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

如需 HTTPS，建议使用 [Certbot](https://certbot.eff.org/) 自动申请并配置 Let's Encrypt 证书。

---

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

---

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
