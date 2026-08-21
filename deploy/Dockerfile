# 基于 Python 3.12 官方精简镜像
FROM python:3.12-slim

# 统一时区（北京），避免事件时间/撤销窗口与浏览器不一致
ENV TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装（利用 Docker 缓存层，requirements 未变更时不重新安装）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码（.env 不打包进镜像，环境变量由 docker-compose 或运行时注入）
COPY . .

# 暴露服务端口
EXPOSE 8000

# 启动 FastAPI 服务，监听所有网络接口
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
