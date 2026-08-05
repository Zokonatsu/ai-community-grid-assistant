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
```

> **注意**：`LLM_API_KEY` 必须替换为真实密钥。`.env` 文件已加入 `.gitignore`，不会上传至代码仓库。

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
# 数据文件路径
cp ./data/events.jsonl ./data/events.jsonl.bak.$(date +%Y%m%d)
```

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

### 6.3 容器安全

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
