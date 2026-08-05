#!/bin/bash
set -e

REPO_URL="https://github.com/Zokonatsu/ai-community-grid-assistant.git"
PROJECT_DIR="ai-community-grid-assistant"

info() {
    echo "[信息] $1"
}

error() {
    echo "[错误] $1" >&2
    exit 1
}

warn() {
    echo "[警告] $1"
}

info "更新软件包列表..."
sudo apt-get update || error "apt-get update 失败"

if ! command -v docker >/dev/null 2>&1; then
    info "安装 Docker..."
    sudo apt-get install -y ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin || error "Docker 安装失败"
    sudo systemctl enable --now docker
    info "Docker 安装完成"
else
    info "Docker 已安装"
fi

if [ -d "$PROJECT_DIR" ]; then
    info "项目目录已存在，进入目录..."
    cd "$PROJECT_DIR"
    info "拉取最新代码..."
    git pull || error "拉取代码失败"
else
    info "拉取项目代码..."
    git clone "$REPO_URL" || error "克隆仓库失败"
    cd "$PROJECT_DIR" || error "进入项目目录失败"
fi

if [ ! -f ".env" ]; then
    warn ".env 文件不存在，请手动配置"
    echo ""
    echo "请在 $(pwd) 目录下创建 .env 文件，配置 LLM_API_KEY 等必要变量。"
    echo ""
    read -rp "配置完成后按回车键继续..."

    if [ ! -f ".env" ]; then
        error ".env 文件未创建，部署中止"
    fi
fi

info "启动服务..."
docker compose up -d --build || error "启动服务失败"

info "部署完成"
