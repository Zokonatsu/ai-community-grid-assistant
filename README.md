# 社区事件处理服务

基于 LangGraph + FastAPI 的社区事件自动分派系统。接收居民原始描述，自动完成信息提取、部门派单和本地持久化。

---

## 技术栈

| 层级 | 技术 |
|:---|:---|
| 工作流编排 | LangGraph（StateGraph） |
| 大模型调用 | OpenAI SDK → DeepSeek API（deepseek-chat） |
| Web 框架 | FastAPI |
| 服务器 | Uvicorn |
| 环境配置 | python-dotenv |

---

## 快速开始

### 1. 克隆项目

```bash
git clone <仓库地址>
cd ai-community-grid-assistant
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

在项目根目录创建 `.env` 文件：

```env
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
```

> **注意**：`LLM_API_KEY` 请替换为你的真实密钥。

### 4. 启动服务

```bash
uvicorn main:app --reload
```

或直接使用 Python：

```bash
python main.py
```

服务默认运行在 `http://127.0.0.1:8000`。

### 5. 访问前端页面

服务启动后，打开浏览器访问：

```
http://127.0.0.1:8000/
```

即可进入 **AI 社区网格员助手** 前端页面，进行事件提交和列表查看。

---

## API 接口

### GET /health

健康检查端点，用于探测服务是否正常运行。

#### 响应体

```json
{
  "status": "ok"
}
```

### GET /api/events

查询所有已处理的事件记录，按创建时间降序排列（最新的在前）。

#### 响应体

```json
[
  {
    "description": "我家楼下下水道堵了",
    "address": "",
    "event_type": "物业维修",
    "urgency": "中",
    "handler": "物业部",
    "status": "已派单",
    "created_at": "2026-07-29 14:30:00"
  }
]
```

> 文件不存在或读取异常时返回空列表 `[]`，不会导致请求失败。

### POST /api/events

提交居民事件描述，系统自动提取信息、分配处理部门并持久化记录。

#### 请求体

```json
{
  "description": "居民事件描述字符串"
}
```

#### 响应体（成功）

```json
{
  "success": true,
  "data": {
    "address": "",
    "event_type": "物业维修",
    "urgency": "中",
    "handler": "物业部",
    "status": "已派单",
    "created_at": "2026-07-29 14:30:00"
  }
}
```

#### 响应体（失败）

```json
{
  "success": false,
  "error": "事件处理失败：APIConnectionError：..."
}
```

---

## 项目结构

```
ai-community-grid-assistant/
├── main.py              # FastAPI 服务入口，封装 REST API
├── workflow.py          # 完整 LangGraph 工作流（receive → dispatch → record）
├── receive_agent.py     # 接收Agent：调用 DeepSeek API 提取 address / event_type / urgency
├── dispatch_agent.py    # 派发Agent：根据 event_type 和 urgency 分配 handler
├── record_agent.py      # 记录Agent：将结果持久化到 ./data/events.jsonl
├── requirements.txt     # 项目依赖及版本号
├── .env                 # 环境变量（API Key、Base URL）
├── static/
│   └── index.html       # 前端管理页面：事件提交表单与事件列表展示
├── data/
│   └── events.jsonl     # 持久化数据文件（JSON Lines 格式）
└── README.md            # 项目说明文档
```

### 各文件说明

| 文件 | 职责 |
|:---|:---|
| `main.py` | FastAPI 应用，对外暴露 `POST /api/events`，内部调用 `workflow.invoke()` |
| `workflow.py` | 组装完整链路：`START → receive_node → dispatch_node → record_node → END` |
| `receive_agent.py` | 单 Agent 模块，使用 `deepseek-chat` 从居民描述中提取结构化信息 |
| `dispatch_agent.py` | 单 Agent 模块，根据 `event_type` 映射处理部门，`urgency="高"` 时加 `[紧急]` 前缀 |
| `record_agent.py` | 单 Agent 模块，补充 `status="已派单"` 和 `created_at`，追加写入 JSONL 文件 |
| `static/index.html` | 前端管理页面，提供事件提交表单与事件列表展示，通过浏览器直接操作 |

---

## 测试用例

### 用例 1：物业维修（常规）

```bash
curl -X POST http://127.0.0.1:8000/api/events \
  -H "Content-Type: application/json" \
  -d '{"description":"我家楼下下水道堵了"}'
```

**预期响应**：

```json
{
  "success": true,
  "data": {
    "address": "",
    "event_type": "物业维修",
    "urgency": "中",
    "handler": "物业部",
    "status": "已派单",
    "created_at": "2026-07-29 14:30:00"
  }
}
```

### 用例 2：安全隐患（高紧急）

```bash
curl -X POST http://127.0.0.1:8000/api/events \
  -H "Content-Type: application/json" \
  -d '{"description":"燃气泄漏了"}'
```

**预期响应**：

```json
{
  "success": true,
  "data": {
    "address": "",
    "event_type": "安全隐患",
    "urgency": "高",
    "handler": "[紧急]安保部",
    "status": "已派单",
    "created_at": "2026-07-29 14:30:00"
  }
}
```

### 用例 3：公共设施

```bash
curl -X POST http://127.0.0.1:8000/api/events \
  -H "Content-Type: application/json" \
  -d '{"description":"小区东门路灯坏了"}'
```

---

## 数据持久化

所有处理完成的事件记录会以 JSON Lines 格式追加写入 `./data/events.jsonl`，每行一条记录：

```json
{"description":"我家楼下下水道堵了","address":"","event_type":"物业维修","urgency":"中","handler":"物业部","status":"已派单","created_at":"2026-07-29 14:30:00"}
```

---

## 工作流链路

```
居民描述
   │
   ▼
┌─────────────────┐
│  receive_node   │  ← 调用 DeepSeek API 提取 address / event_type / urgency
└─────────────────┘
   │
   ▼
┌─────────────────┐
│  dispatch_node  │  ← 根据 event_type 分配 handler，urgency="高" 加 [紧急] 前缀
└─────────────────┘
   │
   ▼
┌─────────────────┐
│   record_node   │  ← 补充 status / created_at，写入 ./data/events.jsonl
└─────────────────┘
   │
   ▼
返回派单结果
```
