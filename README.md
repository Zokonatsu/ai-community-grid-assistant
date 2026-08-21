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

在项目根目录创建 `.env` 文件（模板见 `.env.example`）：

```env
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# 账号数据加密密钥（必填，缺失则服务拒绝启动）
DATA_ENCRYPTION_KEY=<64 位十六进制>
```

> **注意**：`LLM_API_KEY` 请替换为你的真实密钥。
> `DATA_ENCRYPTION_KEY` 用于加密账号/会话数据，生成方式：
> `python -c "import secrets; print(secrets.token_hex(32))"`。密钥需单独备份，丢失即账号数据永久无法解密。

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
├── auth.py              # 用户注册/登录/Token 校验（账号数据加密存取）
├── secure_store.py      # AES-256-GCM 加密存储封装（含密钥生成/轮换 CLI）
├── config.py            # 集中配置：校验 LLM_API_KEY、DATA_ENCRYPTION_KEY
├── requirements.txt     # 项目依赖及版本号
├── .env                 # 环境变量（API Key、加密密钥）
├── static/
│   ├── index.html       # 前端页面：事件提交与列表展示
│   ├── login.html       # 登录/注册页
│   └── admin.html       # 管理后台（审核、派单查看）
├── data/
│   └── events.jsonl     # 事件记录（JSON Lines 格式，明文）
├── secure/
│   ├── users.json.enc      # 账号数据（AES-256-GCM 加密）
│   └── sessions.json.enc   # 会话 Token（AES-256-GCM 加密）
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

---

## 回归测试（pytest：core / full）

测试基建：`tests/` 下 20 个独立测试脚本已改造为「可被 pytest 收集 + 可直跑」折中兼容
（同一套校验逻辑，pytest 与 `python tests/<name>.py` 直跑等价）。`tests/conftest.py`
统一固定测试环境（`AUTH_STORE=file`、64 位 hex 测试密钥、LLM 测试变量，不读 `.env`），
并提供 data/secure 数据隔离（try/finally 崩溃保护）与公共 helper（resident_pair /
admin_token / event_seed）。`tests/run_regression.py` 为统一回归 runner
（core / full / cov 三档，严格串行）。

```bash
# 1) 安装依赖（requirements.txt 已含 pytest / pytest-cov）
pip install -r requirements.txt

# 2) 统一回归（推荐）
python tests/run_regression.py core      # 11 个 P0 核心脚本（CI 必跑）
python tests/run_regression.py full      # 全部 20 个脚本（test_server 默认 skip）
python tests/run_regression.py cov       # 核心模块覆盖率（auth/cloud_store/main）

# 3) pytest 直接收集/运行（与 runner 同一套校验）
python -m pytest tests --collect-only -q
python -m pytest tests -q

# 4) 核心高风险模块覆盖率（auth/cloud_store/main 语句覆盖率 >=80%）
python -m pytest --cov=auth --cov=cloud_store --cov=main --cov-report=term-missing tests -q

# 5) 单脚本直跑兼容（校验逻辑与 pytest 共用）
python tests/test_auth.py
python tests/test_cloud_store.py
python tests/test_security_authorization.py
python tests/test_chain_breaks.py
python tests/test_mutation_effectiveness.py
# 其余脚本同理
```

说明：

- `core`（P0）：test_auth / test_security_fixes / test_data_isolation / test_cloud_store /
  test_register_location / test_event_cancel / test_comprehensive / test_semantic_timeout /
  test_input_validation / test_security_authorization / test_chain_breaks。
- `full`：全部 20 个脚本；`test_server.py` 为远程部署冒烟（需真实服务器），默认 skip；
  `test_mutation_effectiveness.py`（造错有效性校验）仅入 full；
  `test_infra_strength.py`（conftest 崩溃保护强度测试：失败不残留备份、data/secure 原样）仅入 full。
- 注：单进程 `python -m pytest tests -q` 存在既有兼容问题（test_scene_tag 的
  `main.receive_node` 绑定在 import 期，先于它导入 main 的模块会污染其 API 全链路用例，
  与 T-B 无关，可复现于 `pytest tests/test_auth.py tests/test_scene_tag.py`）；
  覆盖率请用上表第 4 条命令（--ignore=tests/test_scene_tag.py）或 runner 的 cov 子命令。
- 数据隔离：每个脚本运行前后自动备份/恢复 `data/` 与 `secure/`，异常崩溃也恢复；
  运行结束后应无 `*.bak.*` 残留（runner 会检查）。
- 密钥安全：测试仅使用固定测试值，真实 COS 密钥 / DATA_ENCRYPTION_KEY 只存在于 `.env`，
  不写入任何测试文件、文档或输出。

## run_tests.py 与 test_server.py 定位

- `tests/run_tests.py`：**实时冒烟脚本**（非 pytest 回归套件）。前置条件：服务需已启动
  （默认 `127.0.0.1:8000`）且脚本内 TOKEN 有效；行为是直接对运行中的服务发 HTTP 请求，
  验证输入稳定性（多轮投票 / 置信度 / 待审核）。回归请用
  `python tests/run_regression.py core / full`（pytest 套件，含数据隔离与崩溃保护）。
- `tests/test_server.py`：**远程部署主机冒烟**（目标 `http://118.31.58.191:8000`）。
  需真实部署服务在跑，本地默认 `skip`（`pytestmark = pytest.mark.skip`），**不入 CI**
  （core/full 均不实际执行，仅收集时可见）。

## CI（GitHub Actions）

CI 配置已就绪（`.github/workflows/`），仓库当前为本地，**推送到 GitHub 后自动生效**；全程离线、不注入 secrets。

### 自动 CI（.github/workflows/ci.yml）

- 触发：`push` / `pull_request`（全分支）。
- job `regression`：checkout → setup-python 3.12 → `pip install -r requirements.txt` →
  `python -m pytest tests --collect-only -q`（收集 sanity）→ `python tests/run_regression.py core`（11 个 P0）。
- job `frontend-security`：checkout → setup-python 3.12 + setup-node 20 →
  `python scripts/check_frontend_js.py`（三页面内联脚本 `node --check`）→
  `python scripts/scan_secrets.py`（密钥泄漏扫描 + `.env` gitignore 校验）。
- 两 job 均不注入任何 secrets / 环境密钥；测试依赖 `tests/conftest.py` 固定测试环境，不读 `.env`。

### 手动云端集成验证（.github/workflows/cloud-integration.yml）

- 仅 `workflow_dispatch` 手动触发（默认任何 push/PR 都不跑）。
- 用途：对 COS 存储桶做「上传(加密) → 下载+解密比对 → 删除临时对象 `_ci_verify_<uuid>.enc`」冒烟，
  验证云端读写与加密链路；**绝不触碰** `users.json.enc` / `sessions.json.enc`，不做数据迁移。
- 使用前需在仓库 **Settings → Secrets and variables → Actions** 配置 5 个变量（仓库文件不含真实值）：
  `COS_REGION`、`COS_BUCKET`、`COS_SECRET_ID`、`COS_SECRET_KEY`、`DATA_ENCRYPTION_KEY`
  （`AUTH_STORE=cloudbase` 由 workflow 注入）。

### 本地等价命令（无需 GitHub）

```bash
python tests/run_regression.py core        # CI regression 主命令等价
python scripts/check_frontend_js.py        # 前端内联脚本语法检查
python scripts/scan_secrets.py             # 密钥泄漏扫描（0 命中 + .env gitignore 校验）
python scripts/verify_cloud_integration.py --offline   # 云端验证逻辑离线单测（mock，不触网）
```

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

## 账号数据加密存储

用户账号与会话数据（用户名、手机号、身份证号、会话 Token 等）使用 **AES-256-GCM** 加密后存入 `secure/` 目录（`users.json.enc`、`sessions.json.enc`），密钥来自环境变量 `DATA_ENCRYPTION_KEY`。

默认以云存储为权威（`.env` 中 `AUTH_STORE=cloudbase`，数据加密后写入腾讯云 COS 对象 `users.json.enc` / `sessions.json.enc`）。**开发/本地测试如需本地模式**：启动前临时设置 `AUTH_STORE=file`（如 PowerShell：`$env:AUTH_STORE='file'`）或使用独立 `.env` 即可改回读写本地 `secure/`，无需连云。

- 密码仍以 PBKDF2+HMAC-SHA256 哈希存储，不存明文。
- 加密文件二进制自包含（魔数 + nonce + 密文），目录或文件被泄露时无法直接读取内容。
- 解密失败或密钥不匹配时服务**拒绝启动**（fail-fast），绝不静默重建数据造成覆盖。
- 密钥工具：
  ```bash
  python secure_store.py genkey                    # 生成新密钥
  DATA_ENCRYPTION_KEY=<旧密钥> python secure_store.py rekey --new <新密钥>   # 轮换密钥（重加密）
  ```
- 首次从旧版本升级时，`data/users.json` 等明文数据会自动加密迁移到 `secure/`，原文件改名为 `*.migrated.bak`。

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
