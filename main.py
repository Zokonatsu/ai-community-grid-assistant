"""
main.py
社区事件处理服务入口（FastAPI REST API 封装）

功能：将 LangGraph 完整工作流（workflow.py）封装为 HTTP REST API 服务，
      对外提供统一的事件提交接口，内部复用已有的 receive→dispatch→record 链路。
      支持异步事件处理：POST 立即返回确认，后台执行工作流，支持 60 秒超时保护。
      任务状态持久化到 tasks.json，服务重启后可恢复；全量事件（含超时/失败）在列表可见。
"""

import config  # noqa: F401  最先加载，确保环境变量在后续导入前就绪

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 复用已有工作流与持久化配置
from workflow import workflow, WorkflowState, dispatch_record_workflow
import record_agent
from receive_agent import _is_valid_input, receive_node, _check_hard_rules_first
import auth

logger = logging.getLogger("main")

# 确保静态文件目录存在
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(STATIC_DIR, exist_ok=True)

# ------------------------------------------------------------------
# 任务状态持久化配置
# ------------------------------------------------------------------
DATA_DIR = "./data"
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")


def _ensure_data_dir() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_tasks() -> dict[str, dict[str, Any]]:
    """
    从磁盘加载任务状态。文件不存在或损坏时返回空字典。
    """
    if not os.path.exists(TASKS_FILE):
        return {}
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.error("加载任务状态文件失败，将使用空状态。异常=%s", exc)
        return {}


def _save_tasks(tasks: dict[str, dict[str, Any]]) -> None:
    """
    将全量任务状态写入磁盘。调用方需自行保证并发安全（在外层锁内调用）。
    """
    _ensure_data_dir()
    try:
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(tasks, f, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as exc:
        logger.error("持久化任务状态失败，文件='%s'，异常=%s", TASKS_FILE, exc)


# ------------------------------------------------------------------
# 异步任务状态管理
# ------------------------------------------------------------------
# 内存任务状态，服务启动时从磁盘恢复
_tasks: dict[str, dict[str, Any]] = _load_tasks()

# 若服务重启前存在未完成的任务，将其标记为失败，避免僵尸任务
if _tasks:
    recovered_count = 0
    for task in _tasks.values():
        if task.get("status") == "处理中":
            task["status"] = "处理失败"
            task["error"] = "服务重启，处理中断，请重新提交"
            recovered_count += 1
    if recovered_count:
        _save_tasks(_tasks)
        logger.info("服务启动：已将 %d 条未完成任务标记为失败", recovered_count)

# 并发锁：保护内存状态更新与文件写入
_task_lock = asyncio.Lock()

# 持有后台任务引用，防止被垃圾回收并避免 "never retrieved" 警告
_background_tasks: set[asyncio.Task] = set()


async def _process_event(
    event_id: str,
    pre_checked_state: dict[str, str],
    user_id: str,
) -> None:
    """
    后台异步执行简化工作流（跳过语义校验，直接派单+记录）。

    语义校验已在 create_event 同步完成并复用其结果，
    后台仅执行 dispatch_node → record_node，避免二次调用 LLM API。
    超时保护：若 dispatch_record_workflow.invoke 超过 60 秒未完成，标记为处理超时。
    """

    def _run() -> dict[str, str]:
        initial_state: WorkflowState = {
            "description": pre_checked_state["description"],
            "address": pre_checked_state.get("address", ""),
            "event_type": pre_checked_state.get("event_type", ""),
            "urgency": pre_checked_state.get("urgency", ""),
            "scene_tag": pre_checked_state.get("scene_tag", ""),
            "handler": "",
            "status": "",
            "created_at": "",
            "user_id": user_id,
            "confidence": pre_checked_state.get("confidence", ""),
        }
        return dispatch_record_workflow.invoke(initial_state)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run),
            timeout=60.0,
        )
        async with _task_lock:
            task = _tasks.get(event_id)
            if task is None or task["status"] != "处理中":
                return
            task.update({
                "status": "已完成",
                "address": result["address"],
                "event_type": result["event_type"],
                "urgency": result["urgency"],
                "scene_tag": result["scene_tag"],
                "handler": result["handler"],
                "completed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            _save_tasks(_tasks)
    except asyncio.TimeoutError:
        async with _task_lock:
            task = _tasks.get(event_id)
            if task is not None:
                task["status"] = "处理超时"
                task["error"] = "AI 处理超过60秒，已超时"
                _save_tasks(_tasks)
        logger.warning("事件处理超时，event_id=%s", event_id)
    except Exception as exc:
        async with _task_lock:
            task = _tasks.get(event_id)
            if task is not None:
                task["status"] = "处理失败"
                task["error"] = f"{type(exc).__name__}：{exc}"
                _save_tasks(_tasks)
        logger.error("事件处理失败，event_id=%s，异常=%s", event_id, exc)


# ------------------------------------------------------------------
# FastAPI 应用实例
# ------------------------------------------------------------------
app = FastAPI(
    title="社区事件处理服务",
    description="接收居民事件描述，自动完成信息提取、派单分配和持久化记录（支持异步处理）",
    version="1.1.0",
)

# 注册 CORS 中间件，允许前端跨域调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Pydantic 请求/响应模型
# ------------------------------------------------------------------
class EventRequest(BaseModel):
    """
    事件提交请求体。
    """
    description: str = Field(..., description="居民事件描述字符串", min_length=1)


class EventResponseData(BaseModel):
    """
    事件处理成功后的业务数据。
    增加 event_id，用于后续查询处理状态。
    """
    event_id: str
    address: str
    event_type: str
    urgency: str
    scene_tag: str
    handler: str
    status: str
    created_at: str


class EventResponse(BaseModel):
    """
    统一响应体。
    """
    success: bool
    data: EventResponseData | None = None
    error: str | None = None


class EventStatusResponse(BaseModel):
    """
    按事件标识查询的响应体。
    """
    event_id: str
    description: str
    status: str
    address: str | None = None
    event_type: str | None = None
    urgency: str | None = None
    scene_tag: str | None = None
    handler: str | None = None
    created_at: str
    completed_at: str | None = None
    error: str | None = None


# ------------------------------------------------------------------
# 认证相关 Pydantic 请求/响应模型
# ------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=6)
    real_name: str = Field(..., min_length=1, max_length=20)
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    role: str = Field(default="resident", pattern=r"^(resident|admin)$")


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class UserInfo(BaseModel):
    id: str
    username: str
    real_name: str
    phone: str
    role: str
    created_at: str


# ------------------------------------------------------------------
# 认证依赖
# ------------------------------------------------------------------
def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def get_current_user_dependency(authorization: str | None = Header(None)) -> dict[str, Any]:
    token = _extract_token(authorization)
    user = auth.get_current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    return user


async def get_admin_dependency(authorization: str | None = Header(None)) -> dict[str, Any]:
    token = _extract_token(authorization)
    user = auth.get_current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="权限不足，仅管理员可访问")
    return user


# 可选认证（用于兼容场景，未登录也允许但可获取用户信息）
async def get_optional_user(authorization: str | None = Header(None)) -> dict[str, Any] | None:
    token = _extract_token(authorization)
    return auth.get_current_user(token)


# ------------------------------------------------------------------
# API 端点：GET /health
# ------------------------------------------------------------------
@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    健康检查端点。
    """
    return {"status": "ok"}


# ------------------------------------------------------------------
# API 端点：认证相关
# ------------------------------------------------------------------
@app.post("/api/auth/register", response_model=AuthResponse)
async def register(request: RegisterRequest) -> AuthResponse:
    """
    用户注册，仅支持居民角色。
    注册时收集真实姓名和手机号作为实名信息。
    """
    if request.role == "admin":
        return AuthResponse(success=False, error="禁止通过注册创建管理员账号")
    success, message, user = auth.register_user(
        username=request.username,
        password=request.password,
        real_name=request.real_name,
        phone=request.phone,
        role=request.role,
    )
    if not success:
        return AuthResponse(success=False, error=message)
    return AuthResponse(success=True, data={"user": user}, error=message)


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: LoginRequest) -> AuthResponse:
    """
    用户登录，返回 Token 和用户信息。
    """
    success, message, result = auth.login_user(
        username=request.username,
        password=request.password,
    )
    if not success:
        return AuthResponse(success=False, error=message)
    return AuthResponse(success=True, data=result, error=message)


@app.post("/api/auth/logout")
async def logout(authorization: str | None = Header(None)) -> dict[str, str]:
    """
    用户登出：使当前 Token 在服务端立即失效。
    """
    token = _extract_token(authorization)
    auth.logout_user(token)
    return {"message": "登出成功"}


@app.get("/api/auth/me")
async def me(current_user: dict[str, Any] = Depends(get_current_user_dependency)) -> UserInfo:
    """
    获取当前登录用户信息。
    """
    return UserInfo(**current_user)


# ------------------------------------------------------------------
# API 端点：GET /api/events
# ------------------------------------------------------------------
@app.get("/api/events")
async def list_events(current_user: dict[str, Any] = Depends(get_current_user_dependency)) -> list[dict[str, str]]:
    """
    查询所有事件记录（含处理中、已完成、处理超时、处理失败）。
    按 created_at 降序排列，最新的在前。
    """
    events: list[dict[str, str]] = []

    async with _task_lock:
        for task in _tasks.values():
            if current_user.get("role") != "admin" and task.get("user_id") != current_user.get("id"):
                continue
            events.append({
                "description": task["description"],
                "address": task.get("address", ""),
                "event_type": task.get("event_type", ""),
                "urgency": task.get("urgency", ""),
                "scene_tag": task.get("scene_tag", ""),
                "handler": task.get("handler", ""),
                "status": task["status"],
                "created_at": task["created_at"],
            })

    # 按 created_at 降序排列，最新的记录展示在最前面
    events.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return events


# ------------------------------------------------------------------
# API 端点：POST /api/events
# ------------------------------------------------------------------
@app.post("/api/events", response_model=EventResponse)
async def create_event(
    request: EventRequest,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> EventResponse:
    """
    提交居民事件，立即返回确认，后台异步执行工作流。
    若 60 秒内未完成，自动标记为处理超时。
    """
    try:
        # 前置快速校验：无效输入直接拒绝，不创建任务，不调用外部API，不留任何记录
        if not _is_valid_input(request.description):
            logger.warning("前置快速校验拦截：description='%s'", request.description)
            return EventResponse(
                success=False,
                error="输入内容无效（如纯问候、闲聊或无实质内容的描述），请提供具体的社区事务描述",
            )

        # ------------------------------------------------------------------
        # 前置硬规则检查（生命安全优先）：命中则跳过所有LLM调用，直接派单
        # ------------------------------------------------------------------
        hard_rule_result = _check_hard_rules_first(request.description)
        if hard_rule_result is not None:
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = {
                    "event_id": event_id,
                    "description": request.description,
                    "status": "处理中",
                    "address": "",
                    "event_type": hard_rule_result["event_type"],
                    "urgency": hard_rule_result["urgency"],
                    "scene_tag": hard_rule_result["scene_tag"],
                    "handler": "",
                    "created_at": created_at,
                    "completed_at": None,
                    "error": None,
                    "user_id": current_user["id"],
                }
                _save_tasks(_tasks)
            # 启动后台异步任务
            bg_task = asyncio.create_task(
                _process_event(event_id, hard_rule_result, current_user["id"])
            )
            _background_tasks.add(bg_task)
            bg_task.add_done_callback(_background_tasks.discard)
            return EventResponse(
                success=True,
                data=EventResponseData(
                    event_id=event_id,
                    address="",
                    event_type=hard_rule_result["event_type"],
                    urgency=hard_rule_result["urgency"],
                    scene_tag=hard_rule_result["scene_tag"],
                    handler="",
                    status="处理中",
                    created_at=created_at,
                ),
            )

        # ------------------------------------------------------------------
        # 同步语义校验（唯一一次）：多轮采样消除随机性
        # ------------------------------------------------------------------
        semantic_result: dict[str, str] | None = None
        try:
            check_state = {
                "description": request.description,
                "address": "",
                "event_type": "",
                "urgency": "",
                "scene_tag": "",
                "handler": "",
                "confidence": "",
            }
            semantic_result = await asyncio.wait_for(
                asyncio.to_thread(receive_node, check_state),
                timeout=50.0,  # 3轮×15秒 ≈ 45秒，留5秒余量
            )
        except asyncio.TimeoutError:
            logger.warning("语义校验超时，创建待审核事件：description='%s'", request.description)
            # 超时无法判断语义，创建待审核事件转人工处理
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = {
                    "event_id": event_id,
                    "description": request.description,
                    "status": "待审核",
                    "address": "",
                    "event_type": "待审核",
                    "urgency": "中",
                    "scene_tag": "常规",
                    "handler": "",
                    "created_at": created_at,
                    "completed_at": None,
                    "error": "语义校验超时，已转人工审核",
                    "user_id": current_user["id"],
                }
                _save_tasks(_tasks)
            return EventResponse(
                success=True,
                data=EventResponseData(
                    event_id=event_id,
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    handler="",
                    status="待审核",
                    created_at=created_at,
                ),
            )
        except Exception as exc:
            logger.error("语义校验异常：description='%s'，异常=%s", request.description, exc)
            # 异常时fallback到待审核，不丢弃消息
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = {
                    "event_id": event_id,
                    "description": request.description,
                    "status": "待审核",
                    "address": "",
                    "event_type": "待审核",
                    "urgency": "中",
                    "scene_tag": "常规",
                    "handler": "",
                    "created_at": created_at,
                    "completed_at": None,
                    "error": f"语义校验异常，已转人工审核：{type(exc).__name__}",
                    "user_id": current_user["id"],
                }
                _save_tasks(_tasks)
            return EventResponse(
                success=True,
                data=EventResponseData(
                    event_id=event_id,
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    handler="",
                    status="待审核",
                    created_at=created_at,
                ),
            )

        # 语义校验完成，根据结果分流处理
        event_type = semantic_result.get("event_type", "")

        if event_type == "无效输入":
            logger.warning("语义校验拦截：description='%s'", request.description)
            return EventResponse(
                success=False,
                error="输入内容无效（如纯问候、闲聊或无实质内容的描述），请提供具体的社区事务描述",
            )

        if event_type == "API异常":
            logger.error("语义校验API异常：description='%s'", request.description)
            # API异常时fallback到待审核，不丢弃消息
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = {
                    "event_id": event_id,
                    "description": request.description,
                    "status": "待审核",
                    "address": "",
                    "event_type": "待审核",
                    "urgency": "中",
                    "scene_tag": "常规",
                    "handler": "",
                    "created_at": created_at,
                    "completed_at": None,
                    "error": "语义校验服务异常，已转人工审核",
                    "user_id": current_user["id"],
                }
                _save_tasks(_tasks)
            return EventResponse(
                success=True,
                data=EventResponseData(
                    event_id=event_id,
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    handler="",
                    status="待审核",
                    created_at=created_at,
                ),
            )

        if event_type == "待审核":
            # 置信度低或地址缺失，创建待审核事件，不走后台工作流
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = {
                    "event_id": event_id,
                    "description": request.description,
                    "status": "待审核",
                    "address": semantic_result.get("address", ""),
                    "event_type": "待审核",
                    "urgency": semantic_result.get("urgency", "中"),
                    "scene_tag": semantic_result.get("scene_tag", "常规"),
                    "handler": "",
                    "created_at": created_at,
                    "completed_at": None,
                    "error": None,
                    "user_id": current_user["id"],
                }
                _save_tasks(_tasks)
            return EventResponse(
                success=True,
                data=EventResponseData(
                    event_id=event_id,
                    address=semantic_result.get("address", ""),
                    event_type="待审核",
                    urgency=semantic_result.get("urgency", "中"),
                    scene_tag=semantic_result.get("scene_tag", "常规"),
                    handler="",
                    status="待审核",
                    created_at=created_at,
                ),
            )

        # ------------------------------------------------------------------
        # 有效输入：创建处理中任务，后台只走派单+记录（复用同步校验结果）
        # ------------------------------------------------------------------
        event_id = str(uuid.uuid4())
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        async with _task_lock:
            _tasks[event_id] = {
                "event_id": event_id,
                "description": request.description,
                "status": "处理中",
                "address": semantic_result.get("address", ""),
                "event_type": semantic_result.get("event_type", ""),
                "urgency": semantic_result.get("urgency", ""),
                "scene_tag": semantic_result.get("scene_tag", ""),
                "handler": "",
                "created_at": created_at,
                "completed_at": None,
                "error": None,
                "user_id": current_user["id"],
            }
            _save_tasks(_tasks)

        # 启动后台异步任务，传入已校验结果，避免二次调用 LLM API
        bg_task = asyncio.create_task(
            _process_event(event_id, semantic_result, current_user["id"])
        )
        _background_tasks.add(bg_task)
        bg_task.add_done_callback(_background_tasks.discard)

        # 立即返回确认信息
        return EventResponse(
            success=True,
            data=EventResponseData(
                event_id=event_id,
                address=semantic_result.get("address", ""),
                event_type=semantic_result.get("event_type", ""),
                urgency=semantic_result.get("urgency", ""),
                scene_tag=semantic_result.get("scene_tag", ""),
                handler="",
                status="处理中",
                created_at=created_at,
            ),
        )

    except Exception as exc:
        # 最后兜底：生命急救/紧急救援消息绝不丢弃
        hard = _check_hard_rules_first(request.description)
        if hard is not None:
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = {
                    "event_id": event_id,
                    "description": request.description,
                    "status": "待审核",
                    "address": "",
                    "event_type": "待审核",
                    "urgency": "高",
                    "scene_tag": hard["scene_tag"],
                    "handler": "",
                    "created_at": created_at,
                    "completed_at": None,
                    "error": f"处理异常已转人工：{type(exc).__name__}",
                    "user_id": current_user["id"],
                }
                _save_tasks(_tasks)
            return EventResponse(
                success=True,
                data=EventResponseData(
                    event_id=event_id,
                    address="",
                    event_type="待审核",
                    urgency="高",
                    scene_tag=hard["scene_tag"],
                    handler="",
                    status="待审核",
                    created_at=created_at,
                ),
            )
        return EventResponse(
            success=False,
            error=f"事件提交失败：{type(exc).__name__}：{exc}",
        )


# ------------------------------------------------------------------
# API 端点：GET /api/events/{event_id}
# ------------------------------------------------------------------
@app.get("/api/events/{event_id}", response_model=EventStatusResponse)
async def get_event(
    event_id: str,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> EventStatusResponse:
    """
    按事件标识查询处理状态和完整结果。服务重启后仍可通过本接口恢复查询。
    """
    async with _task_lock:
        task = _tasks.get(event_id)

    if task is None:
        raise HTTPException(status_code=404, detail="事件不存在")

    if current_user.get("role") != "admin" and task.get("user_id") != current_user.get("id"):
        raise HTTPException(status_code=403, detail="无权访问该事件")

    return EventStatusResponse(
        event_id=task["event_id"],
        description=task["description"],
        status=task["status"],
        address=task.get("address") or None,
        event_type=task.get("event_type") or None,
        urgency=task.get("urgency") or None,
        scene_tag=task.get("scene_tag") or None,
        handler=task.get("handler") or None,
        created_at=task["created_at"],
        completed_at=task.get("completed_at"),
        error=task.get("error"),
    )


# ------------------------------------------------------------------
# 静态文件托管
# ------------------------------------------------------------------
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ------------------------------------------------------------------
# 主程序入口
# ------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info",
    )
