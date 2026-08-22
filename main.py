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
import re
import uuid
from datetime import datetime
from typing import Any
from log_redact import redact_pii

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 限流（slowapi）与统一 429 响应（T20260821-004）
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

# Prometheus 监控指标（T20260821-005）：暴露 GET /metrics
from prometheus_fastapi_instrumentator import Instrumentator, metrics

# 复用已有工作流与持久化配置
from workflow import workflow, WorkflowState, dispatch_record_workflow
import record_agent
from receive_agent import receive_node, _check_hard_rules_first, _check_fuzzy_emergency
import receive_agent  # noqa: F811  用于调试：确认加载的模块路径
import dispatch_agent
import auth
import geo
import community_store
# 字段级加解密（tasks.json at-rest 加密，T20260821-003）
from secure_store import (
    TASK_ENCRYPT_FIELDS,
    TASK_NUMERIC_FIELDS,
    encrypt_record_fields,
    decrypt_record_fields,
    atomic_write,
)

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
    从磁盘加载任务状态。文件不存在或 JSON 损坏时返回空字典。

    T20260821-003：加载后对敏感字段透明解密，内存 _tasks 保持明文；
    解密失败（密钥不匹配/密文被篡改损坏）raise SecureStoreError（fail-fast，
    与账号数据一致，禁止静默丢数据）。
    """
    if not os.path.exists(TASKS_FILE):
        return {}
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.error("加载任务状态文件失败，将使用空状态。异常=%s", exc)
        return {}
    if not isinstance(data, dict):
        return {}
    for task_id, task in data.items():
        if isinstance(task, dict):
            data[task_id] = decrypt_record_fields(task, TASK_ENCRYPT_FIELDS, TASK_NUMERIC_FIELDS)
    return data


def _save_tasks(tasks: dict[str, dict[str, Any]]) -> None:
    """
    将全量任务状态写入磁盘。调用方需自行保证并发安全（在外层锁内调用）。

    T20260821-003：落盘前对敏感字段加密（仅加密副本，不改动内存明文 _tasks；
    读回时 _load_tasks 透明解密，API/管理端逻辑零改动）。
    """
    _ensure_data_dir()
    try:
        disk_tasks = {
            task_id: encrypt_record_fields(task, TASK_ENCRYPT_FIELDS, TASK_NUMERIC_FIELDS)
            for task_id, task in tasks.items()
        }
        blob = json.dumps(disk_tasks, ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write(TASKS_FILE, blob)
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

# 数据兼容：将旧版的单条 reply 字符串迁移为 replies 列表
for task in _tasks.values():
    if "replies" not in task:
        task["replies"] = []
        if task.get("reply"):
            task["replies"].append({
                "content": task["reply"],
                "created_at": task.get("completed_at", task.get("created_at", "")),
                "reviewer_id": task.get("reviewer_id", ""),
                "reviewer_name": "",
            })
    if "user_read_at" not in task:
        task["user_read_at"] = ""

# 并发锁：保护内存状态更新与文件写入
_task_lock = asyncio.Lock()

# 持有后台任务引用，防止被垃圾回收并避免 "never retrieved" 警告
_background_tasks: set[asyncio.Task] = set()


async def _process_event(
    event_id: str,
    pre_checked_state: dict[str, str],
    user_id: str,
    lat: float | None = None,
    lng: float | None = None,
) -> None:
    """
    后台异步执行简化工作流（跳过语义校验，直接派单+记录）。

    语义校验已在 create_event 同步完成并复用其结果，
    后台仅执行 dispatch_node → record_node，避免二次调用 LLM API。
    超时保护：若 dispatch_record_workflow.invoke 超过 60 秒未完成，标记为处理超时。
    定位坐标 lat/lng 仅透传给 record_node 落盘，不参与派单决策。
    """

    def _run() -> dict[str, str]:
        initial_state: WorkflowState = {
            "description": pre_checked_state["description"],
            "address": pre_checked_state.get("address", ""),
            "event_type": pre_checked_state.get("event_type", ""),
            "urgency": pre_checked_state.get("urgency", ""),
            "scene_tag": pre_checked_state.get("scene_tag", ""),
            "handler": "",
            "status": pre_checked_state.get("status", ""),
            "created_at": "",
            "user_id": user_id,
            "confidence": pre_checked_state.get("confidence", ""),
            "confirmation_required": pre_checked_state.get("confirmation_required", False),
            "emergency_type": pre_checked_state.get("emergency_type", ""),
            "confirmed": pre_checked_state.get("confirmed", False),
            "lat": lat,
            "lng": lng,
        }
        return dispatch_record_workflow.invoke(initial_state)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run),
            timeout=60.0,
        )
        async with _task_lock:
            task = _tasks.get(event_id)
            if task is None:
                return
            # D2 修复：先校验 invoke 结果、后变更状态，避免「表面成功」
            REQUIRED = ("handler", "address", "event_type", "urgency", "scene_tag")
            if not isinstance(result, dict):
                # 状态守卫：仅处理中/待审核可被改写为处理失败，防止覆盖「已撤销」
                if task.get("status") in ("处理中", "待审核"):
                    task["status"] = "处理失败"
                    task["error"] = "事件处理结果无效"
                    _save_tasks(_tasks)
                logger.error("事件处理结果无效（非 dict），event_id=%s，result=%r", event_id, redact_pii(result))
            elif missing := [k for k in REQUIRED if k not in result]:
                # 状态守卫：仅处理中/待审核可被改写为处理失败，防止覆盖「已撤销」
                if task.get("status") in ("处理中", "待审核"):
                    task["status"] = "处理失败"
                    task["error"] = "事件处理结果缺失必需字段：" + ",".join(missing)
                    _save_tasks(_tasks)
                logger.error("事件处理结果缺失必需字段，event_id=%s，missing=%s", event_id, ",".join(missing))
            else:
                # 处理中事件完成后标记为"已完成"；待审核事件保留原状态
                if task["status"] == "处理中":
                    task["status"] = "已完成"
                task.update({
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
            # 状态守卫：仅处理中/待审核可被改写为处理超时，防止覆盖「已撤销」
            if task is not None and task.get("status") in ("处理中", "待审核"):
                task["status"] = "处理超时"
                task["error"] = "AI 处理超过60秒，已超时"
                _save_tasks(_tasks)
        logger.warning("事件处理超时，event_id=%s", event_id)
    except Exception as exc:
        async with _task_lock:
            task = _tasks.get(event_id)
            # 状态守卫：仅处理中/待审核可被改写为处理失败，防止覆盖「已撤销」
            if task is not None and task.get("status") in ("处理中", "待审核"):
                task["status"] = "处理失败"
                task["error"] = f"{type(exc).__name__}：{exc}"
                _save_tasks(_tasks)
        logger.error("事件处理失败，event_id=%s，异常=%s", event_id, exc)


def _build_task(
    *,
    event_id: str,
    description: str,
    created_at: str,
    status: str,
    address: str,
    event_type: str,
    urgency: str,
    scene_tag: str,
    user: dict[str, Any],
    error: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    beneficiary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    统一构造事件任务字典。

    集中注入提交者实名信息、事件实时定位（含范围内校验结果与距中心米数）、
    被帮助人信息（本人/代人办），避免 create_event 内多处重复手写 dict 导致字段遗漏。
    定位坐标仅随事件存储并供后台查看，不参与派单决策。
    """
    if lat is not None and lng is not None:
        within, _dist = geo.is_within_community(lat, lng)
        location_status = "verified" if within else "unverified"
        event_distance_m = _dist
    else:
        location_status = "unverified"
        event_distance_m = None
    bf = beneficiary or {}
    return {
        "event_id": event_id,
        "description": description,
        "status": status,
        "address": address,
        "event_type": event_type,
        "urgency": urgency,
        "scene_tag": scene_tag,
        "handler": "",
        "created_at": created_at,
        "completed_at": None,
        "error": error,
        "user_id": user["id"],
        "user_name": user.get("real_name", ""),
        "user_phone": user.get("phone", ""),
        "user_id_card": user.get("id_card", ""),
        "user_building": user.get("building", ""),
        "user_unit": user.get("unit", ""),
        "user_room": user.get("room", ""),
        "reply": "",
        "event_lat": lat,
        "event_lng": lng,
        "event_location_status": location_status,
        "event_distance_m": event_distance_m,
        "beneficiary_type": bf.get("beneficiary_type", "self"),
        "beneficiary_name": bf.get("beneficiary_name", user.get("real_name", "")),
        "beneficiary_phone": bf.get("beneficiary_phone", user.get("phone", "")),
        "beneficiary_building": bf.get("beneficiary_building", user.get("building", "")),
        "beneficiary_unit": bf.get("beneficiary_unit", user.get("unit", "")),
        "beneficiary_room": bf.get("beneficiary_room", user.get("room", "")),
    }


def _resolve_beneficiary(request: "EventRequest", user: dict[str, Any]) -> dict[str, Any]:
    """
    根据提交方式解析被帮助人信息。
    self：被帮助人即提交者本人（复用账号住户信息）；proxy：使用请求中被帮助人字段。
    """
    if request.beneficiary_type == "proxy":
        return {
            "beneficiary_type": "proxy",
            "beneficiary_name": (request.beneficiary_name or "").strip(),
            "beneficiary_phone": (request.beneficiary_phone or "").strip(),
            "beneficiary_building": (request.beneficiary_building or "").strip(),
            "beneficiary_unit": (request.beneficiary_unit or "").strip(),
            "beneficiary_room": (request.beneficiary_room or "").strip(),
        }
    return {"beneficiary_type": "self"}


# ------------------------------------------------------------------
# FastAPI 应用实例
# ------------------------------------------------------------------
app = FastAPI(
    title="社区事件处理服务",
    description="接收居民事件描述，自动完成信息提取、派单分配和持久化记录（支持异步处理）",
    version="1.1.0",
)

# 注册 CORS 中间件：来源白名单取自 config.CORS_ALLOW_ORIGINS
# （环境变量 CORS_ALLOW_ORIGINS，逗号分隔；默认放行本机与生产前端域名，见 config.py）。
# 通配 * 与 allow_credentials=True 的组合会被浏览器拒绝，因此列表含 * 时自动降级
# allow_credentials=False 并记录 warning，避免出现「通配 + 凭据」的失控 CORS。
_cors_allow_origins = config.CORS_ALLOW_ORIGINS
_cors_allow_credentials = "*" not in _cors_allow_origins
if not _cors_allow_credentials:
    logger.warning(
        "CORS_ALLOW_ORIGINS 含通配符 *，已自动将 allow_credentials 置为 False"
        "（通配来源 + 携带凭据的组合会被浏览器拒绝）。建议配置为显式域名白名单。"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# 限流（slowapi，T20260821-004）
# ------------------------------------------------------------------
# 单机内存限流：登录/注册按客户端 IP（RATE_LIMIT_LOGIN，默认 5/minute），
# POST /api/events 按 Bearer token 内 user_id（无有效 token 按 IP，
# RATE_LIMIT_EVENTS，默认 10/minute）。超限统一返回 HTTP 429 + JSON
# {"detail": "请求过于频繁，请稍后再试"}（自定义 exception handler，见下）。
# RATE_LIMIT_ENABLED=false（测试环境）时 Limiter 整体禁用，limit 装饰器空转。
limiter = Limiter(
    key_func=get_remote_address,
    enabled=config.RATE_LIMIT_ENABLED,
)
app.state.limiter = limiter


async def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """
    限流超限统一响应：HTTP 429 + 固定 JSON 文案（前端 fetch 错误分支可直接识别）。
    """
    return JSONResponse(
        status_code=429,
        content={"detail": "请求过于频繁，请稍后再试"},
    )


app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ------------------------------------------------------------------
# Prometheus 监控指标（T20260821-005）
# ------------------------------------------------------------------
# 挂载 prometheus-fastapi-instrumentator 默认指标（http_requests_total /
# http_request_duration_seconds / http_request_duration_highr_seconds 等），
# 并暴露 GET /metrics（text/plain; version=0.0.4; charset=utf-8）。
# - 无需鉴权：与业务接口无关，Prometheus 抓取不携带 token；
# - 不参与业务限流：slowapi 仅作用于显式 @limiter.limit 装饰的端点，
#   RATE_LIMIT_ENABLED 不影响 /metrics；
# - include_in_schema=False：不进 OpenAPI 文档。
# 生产建议由反向代理限制 /metrics 仅内网可达（详见 docs/监控告警.md）。
Instrumentator().add(metrics.default()).instrument(app).expose(
    app,
    endpoint="/metrics",
    include_in_schema=False,
)


# ------------------------------------------------------------------
# Pydantic 请求/响应模型
# ------------------------------------------------------------------
class EventRequest(BaseModel):
    """
    事件提交请求体。
    """
    description: str = Field(..., description="居民事件描述字符串", min_length=1, max_length=500)
    confirmed: bool = Field(default=False, description="用户是否已确认高风险描述（用于模糊急救二次提交）")
    emergency_type: str | None = Field(default=None, description="模糊急救类型：medical/police/fire（用于二次提交时传递）")
    lat: float | None = Field(default=None, ge=-90, le=90, description="事件实时定位纬度")
    lng: float | None = Field(default=None, ge=-180, le=180, description="事件实时定位经度")
    # 提交方式：本人（self）/ 代人办（proxy）
    beneficiary_type: str = Field(default="self", description="提交方式：self=本人，proxy=代人办")
    beneficiary_name: str | None = Field(default=None, description="被帮助人姓名（代人办必填）")
    beneficiary_phone: str | None = Field(default=None, description="被帮助人手机号（代人办必填）")
    beneficiary_building: str | None = Field(default=None, description="被帮助人楼栋（代人办必填）")
    beneficiary_unit: str | None = Field(default=None, description="被帮助人单元（代人办必填）")
    beneficiary_room: str | None = Field(default=None, description="被帮助人房间号（代人办必填）")


class CommunityUpdateRequest(BaseModel):
    """
    社区中心设置更新请求体（后台「社区设置」）。
    """
    name: str | None = Field(default=None, description="社区名称")
    center_lat: float = Field(..., ge=-90, le=90, description="中心纬度")
    center_lng: float = Field(..., ge=-180, le=180, description="中心经度")
    radius_m: float = Field(..., gt=0, description="覆盖半径（米）")


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
    confirmation_required: bool | None = Field(default=None, description="是否需要前端二次确认（模糊急救短词触发）")
    emergency_type: str | None = Field(default=None, description="模糊急救类型：medical/police/fire")


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
    reply: str | None = None


# ------------------------------------------------------------------
# 认证相关 Pydantic 请求/响应模型
# ------------------------------------------------------------------
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=6)
    real_name: str = Field(..., min_length=1, max_length=20)
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    id_card: str = Field(default="", max_length=18, description="居民身份证号（可选，非空时校验格式）")
    role: str = Field(default="resident", pattern=r"^(resident|admin)$")
    building: str = Field(default="", max_length=20, description="楼栋（居民注册必填）")
    unit: str = Field(default="", max_length=20, description="单元（居民注册必填）")
    room: str = Field(default="", max_length=20, description="房间号（居民注册必填）")
    register_lat: float | None = Field(default=None, ge=-90, le=90, description="注册时定位纬度")
    register_lng: float | None = Field(default=None, ge=-180, le=180, description="注册时定位经度")


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
    status: str = "active"
    building: str = ""
    unit: str = ""
    room: str = ""
    location_status: str = "unverified"


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
# 真实客户端 IP 获取（支持反向代理）
# ------------------------------------------------------------------
def _real_client_ip(request: Request) -> str:
    """
    获取真实客户端 IP：优先解析反向代理传递的 X-Forwarded-For，
    其次 X-Real-IP，最后回退到直接连接的远程地址。
    """
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        # X-Forwarded-For 可能包含多个 IP，取第一个（最原始的客户端 IP）
        first_ip = x_forwarded_for.split(",")[0].strip()
        if first_ip:
            return first_ip
    x_real_ip = request.headers.get("x-real-ip")
    if x_real_ip:
        return x_real_ip.strip()
    return get_remote_address(request)


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
@limiter.limit(config.RATE_LIMIT_LOGIN, key_func=_real_client_ip)
async def register(request: Request, body: RegisterRequest) -> AuthResponse:
    """
    用户注册，仅支持居民角色。
    注册时收集真实姓名和手机号作为实名信息。
    """
    if body.role == "admin":
        return AuthResponse(success=False, error="禁止通过注册创建管理员账号")
    success, message, user = auth.register_user(
        username=body.username,
        password=body.password,
        real_name=body.real_name,
        phone=body.phone,
        id_card=body.id_card,
        role=body.role,
        building=body.building,
        unit=body.unit,
        room=body.room,
        register_lat=body.register_lat,
        register_lng=body.register_lng,
    )
    if not success:
        return AuthResponse(success=False, error=message)
    return AuthResponse(success=True, data={"user": user}, error=message)


@app.post("/api/auth/login", response_model=AuthResponse)
@limiter.limit(config.RATE_LIMIT_LOGIN, key_func=_real_client_ip)
async def login(request: Request, body: LoginRequest) -> AuthResponse:
    """
    用户登录，返回 Token 和用户信息。
    """
    success, message, result = auth.login_user(
        username=body.username,
        password=body.password,
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
# API 端点：住户列表（管理员，只读）
# ------------------------------------------------------------------
@app.get("/api/admin/users")
async def admin_list_users(
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> list[dict[str, Any]]:
    """
    管理员获取全部住户信息（只读，注册即生效，无审核操作）。
    """
    return auth.list_users()


# ------------------------------------------------------------------
# API 端点：社区名称（公开，前端标题/页头动态显示用）
# ------------------------------------------------------------------
@app.get("/api/community")
async def public_get_community() -> dict[str, Any]:
    '''获取社区名称等公开配置（无需登录，供各页面标题/页头动态显示）。'''
    return geo.get_community_config()


# API 端点：社区中心设置（管理员）
# ------------------------------------------------------------------
@app.get("/api/admin/community")
async def admin_get_community(
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    获取当前社区中心配置（后台「社区设置」展示用）。
    含 name/center_lat/center_lng/radius_m/updated_at。
    """
    return geo.get_community_config()


@app.put("/api/admin/community")
async def admin_update_community(
    request: CommunityUpdateRequest,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    保存社区中心位置（后台「社区设置」），立即生效、无需重启。
    经纬度/半径非法由 Pydantic 校验拦截（422）。
    """
    try:
        config = community_store.save(
            name=request.name or "",
            center_lat=request.center_lat,
            center_lng=request.center_lng,
            radius_m=request.radius_m,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return config


# ------------------------------------------------------------------
# API 端点：GET /api/events
# ------------------------------------------------------------------
@app.get("/api/events")
async def list_events(current_user: dict[str, Any] = Depends(get_current_user_dependency)) -> list[dict[str, Any]]:
    """
    查询所有事件记录（含处理中、已完成、处理超时、处理失败）。
    按 created_at 降序排列，最新的在前。
    """
    events: list[dict[str, str]] = []

    async with _task_lock:
        for task in _tasks.values():
            if current_user.get("role") != "admin" and task.get("user_id") != current_user.get("id"):
                continue
            replies = task.get("replies", [])
            if not replies and task.get("reply"):
                replies = [{
                    "content": task["reply"],
                    "created_at": task.get("completed_at", task.get("created_at", "")),
                    "reviewer_id": task.get("reviewer_id", ""),
                    "reviewer_name": "",
                }]
            has_new_reply = False
            if replies:
                last_reply_at = replies[-1].get("created_at", "")
                user_read_at = task.get("user_read_at", "")
                if not user_read_at or last_reply_at > user_read_at:
                    has_new_reply = True
            event_item: dict[str, Any] = {
                "event_id": task["event_id"],
                "description": task["description"],
                "address": task.get("address", ""),
                "event_type": task.get("event_type", ""),
                "urgency": task.get("urgency", ""),
                "scene_tag": task.get("scene_tag", ""),
                "handler": task.get("handler", ""),
                "status": task["status"],
                "created_at": task["created_at"],
                "reply": task.get("reply", ""),
                "replies": replies,
                "has_new_reply": has_new_reply,
                "user_name": task.get("user_name", ""),
                "user_phone": task.get("user_phone", ""),
                "user_id_card": task.get("user_id_card", ""),
                "user_building": task.get("user_building", ""),
                "user_unit": task.get("user_unit", ""),
                "user_room": task.get("user_room", ""),
                "beneficiary_type": task.get("beneficiary_type", "self"),
                "beneficiary_name": task.get("beneficiary_name", ""),
                "beneficiary_phone": task.get("beneficiary_phone", ""),
                "beneficiary_building": task.get("beneficiary_building", ""),
                "beneficiary_unit": task.get("beneficiary_unit", ""),
                "beneficiary_room": task.get("beneficiary_room", ""),
            }
            # 快照缺失时从用户库实时补齐（老事件无住址快照 / 用户补录住址后生效）
            if not event_item.get("user_building") and task.get("user_id"):
                _u = auth.get_user_by_id(task["user_id"])
                if _u:
                    event_item["user_building"] = _u.get("building", "")
                    event_item["user_unit"] = _u.get("unit", "")
                    event_item["user_room"] = _u.get("room", "")
                    if not event_item.get("user_name"):
                        event_item["user_name"] = _u.get("real_name", "")
                    if not event_item.get("user_phone"):
                        event_item["user_phone"] = _u.get("phone", "")

            # 定位坐标/距中心米数仅管理员可见，居民端不返回（避免暴露他人位置）
            if current_user.get("role") == "admin":
                event_item["event_lat"] = task.get("event_lat")
                event_item["event_lng"] = task.get("event_lng")
                event_item["event_location_status"] = task.get("event_location_status", "unverified")
                event_item["event_distance_m"] = task.get("event_distance_m")
            events.append(event_item)

    # 按 created_at 降序排列，最新的记录展示在最前面
    events.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return events


# ------------------------------------------------------------------
# API 端点：POST /api/events
# ------------------------------------------------------------------
def _events_rate_key(request: Request) -> str:
    """
    POST /api/events 限流键：优先取 Bearer token 对应 user_id；
    无有效 token 或解析失败时按客户端 IP 兜底。
    """
    token = _extract_token(request.headers.get("authorization"))
    if token:
        try:
            user = auth.get_current_user(token)
        except Exception:
            user = None
        if user and user.get("id"):
            return f"user:{user['id']}"
    return f"ip:{get_remote_address(request)}"


@app.post("/api/events", response_model=EventResponse)
@limiter.limit(config.RATE_LIMIT_EVENTS, key_func=_events_rate_key)
async def create_event(
    request: Request,
    payload: EventRequest,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> EventResponse:
    """
    提交居民事件，立即返回确认，后台异步执行工作流。
    若 60 秒内未完成，自动标记为处理超时。
    """
    try:
        print(f"[DEBUG] Loaded receive_agent from: {receive_agent.__file__}")
        # ------------------------------------------------------------------
        # 提交方式校验：本人（self）/ 代人办（proxy）
        # ------------------------------------------------------------------
        if payload.beneficiary_type not in ("self", "proxy"):
            return EventResponse(
                success=False,
                error="提交方式不合法，仅支持本人（self）或代人办（proxy）",
            )
        if payload.beneficiary_type == "proxy":
            missing = []
            for label, val in (
                ("被帮助人姓名", payload.beneficiary_name),
                ("手机号", payload.beneficiary_phone),
                ("楼栋", payload.beneficiary_building),
                ("单元", payload.beneficiary_unit),
                ("房间号", payload.beneficiary_room),
            ):
                if not (val or "").strip():
                    missing.append(label)
            if missing:
                return EventResponse(
                    success=False,
                    error="代人办需填写：" + "、".join(missing),
                )
        beneficiary = _resolve_beneficiary(payload, current_user)
        # ------------------------------------------------------------------
        # 前置硬规则检查（生命安全优先）：命中则跳过所有LLM调用
        # ------------------------------------------------------------------
        hard_rule_result = _check_hard_rules_first(payload.description)
        if hard_rule_result is not None:
            if not payload.confirmed:
                # 未确认：返回弹窗确认，不创建任务
                return EventResponse(
                    success=True,
                    error=f"检测到高风险描述「{payload.description.strip()}」，请确认是否向外部急救资源求助",
                    data=EventResponseData(
                        event_id="",
                        address="",
                        event_type=hard_rule_result["event_type"],
                        urgency=hard_rule_result["urgency"],
                        scene_tag="",
                        handler="",
                        status="",
                        created_at="",
                        confirmation_required=True,
                        emergency_type=hard_rule_result.get("emergency_type", ""),
                    ),
                )
            # 已确认：直接创建任务并派单
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=payload.description,
                    created_at=created_at,
                    status="处理中",
                    address="",
                    event_type=hard_rule_result["event_type"],
                    urgency=hard_rule_result["urgency"],
                    scene_tag=hard_rule_result["scene_tag"],
                    user=current_user,
                    lat=payload.lat,
                    lng=payload.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台异步任务
            bg_task = asyncio.create_task(
                _process_event(event_id, hard_rule_result, current_user["id"], payload.lat, payload.lng)
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
        # 前置模糊急救检查：高风险短词且用户未确认时，返回确认提示，不创建任务
        # ------------------------------------------------------------------
        if not payload.confirmed:
            fuzzy_emergency = _check_fuzzy_emergency(payload.description)
            if fuzzy_emergency is not None:
                logger.warning("前置模糊急救命中（%s），返回确认提示：description='%s'", fuzzy_emergency["emergency_type"], redact_pii(payload.description), )
                return EventResponse(
                    success=True,
                    error=f"检测到高风险关键词「{payload.description.strip()}」，请补充具体地址和详细描述后重新提交",
                    data=EventResponseData(
                        event_id="",
                        address="",
                        event_type="安全隐患",
                        urgency="高",
                        scene_tag="",
                        handler="",
                        status="",
                        created_at="",
                        confirmation_required=True,
                        emergency_type=fuzzy_emergency["emergency_type"],
                    ),
                )

        # ------------------------------------------------------------------
        # 同步语义校验（唯一一次）：LLM 多轮采样投票提取语义
        # ------------------------------------------------------------------
        semantic_result: dict[str, str] | None = None
        try:
            check_state = {
                "description": payload.description,
                "address": "",
                "event_type": "",
                "urgency": "",
                "scene_tag": "",
                "handler": "",
                "confidence": "",
                "confirmation_required": False,
                "emergency_type": payload.emergency_type or "",
                "confirmed": payload.confirmed,
            }
            semantic_result = await asyncio.wait_for(
                asyncio.to_thread(receive_node, check_state),
                timeout=50.0,  # 3轮并行×15秒，留足余量
            )
        except asyncio.TimeoutError:
            logger.warning("语义校验超时，创建待审核事件：description='%s'", redact_pii(payload.description))
            # 超时无法判断语义，创建待审核事件转人工部处理
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=payload.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    user=current_user,
                    error="语义校验超时，已转人工审核",
                    lat=payload.lat,
                    lng=payload.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            timeout_state = {
                "description": payload.description,
                "address": "",
                "event_type": "待审核",
                "urgency": "中",
                "scene_tag": "常规",
                "handler": "",
                "confidence": "none",
                "confirmation_required": False,
                "emergency_type": "人工部",
                "confirmed": False,
                "status": "待审核",
            }
            bg_task = asyncio.create_task(
                _process_event(event_id, timeout_state, current_user["id"], payload.lat, payload.lng)
            )
            _background_tasks.add(bg_task)
            bg_task.add_done_callback(_background_tasks.discard)
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
            logger.error("语义校验异常：description='%s'，异常=%s", redact_pii(payload.description), redact_pii(exc))
            # 异常时fallback到待审核，不丢弃消息
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=payload.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    user=current_user,
                    error=f"语义校验异常，已转人工审核：{type(exc).__name__}",
                    lat=payload.lat,
                    lng=payload.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            exc_state = {
                "description": payload.description,
                "address": "",
                "event_type": "待审核",
                "urgency": "中",
                "scene_tag": "常规",
                "handler": "",
                "confidence": "none",
                "confirmation_required": False,
                "emergency_type": "人工部",
                "confirmed": False,
                "status": "待审核",
            }
            bg_task = asyncio.create_task(
                _process_event(event_id, exc_state, current_user["id"], payload.lat, payload.lng)
            )
            _background_tasks.add(bg_task)
            bg_task.add_done_callback(_background_tasks.discard)
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

        # ------------------------------------------------------------------
        # 语义校验结果守卫（D1 修复）：receive_node 返回 None / 非 dict 时，
        # 复用「API异常」降级路径转待审核，避免对 None 调用 .get() 抛内部异常
        # ------------------------------------------------------------------
        if semantic_result is None or not isinstance(semantic_result, dict):
            logger.error("语义校验返回无效结果：description='%s'，result=%r", redact_pii(payload.description), redact_pii(semantic_result))
            # 复用「API异常」降级路径：建待审核任务（error="语义校验服务异常，已转人工审核"）、
            # 启动 _process_event（emergency_type="人工部"、status="待审核"）、
            # 返回 EventResponse(success=True, data.status="待审核", error=None)
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=payload.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    user=current_user,
                    error="语义校验服务异常，已转人工审核",
                    lat=payload.lat,
                    lng=payload.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            invalid_state = {
                "description": payload.description,
                "address": "",
                "event_type": "待审核",
                "urgency": "中",
                "scene_tag": "常规",
                "handler": "",
                "confidence": "none",
                "confirmation_required": False,
                "emergency_type": "人工部",
                "confirmed": False,
                "status": "待审核",
            }
            bg_task = asyncio.create_task(
                _process_event(event_id, invalid_state, current_user["id"], payload.lat, payload.lng)
            )
            _background_tasks.add(bg_task)
            bg_task.add_done_callback(_background_tasks.discard)
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

        # 兜底：receive_node 返回了模糊急救确认标识（独立调用场景）
        if semantic_result.get("confirmation_required"):
            return EventResponse(
                success=True,
                error=f"检测到高风险关键词「{payload.description.strip()}」，请补充具体地址和详细描述后重新提交",
                data=EventResponseData(
                    event_id="",
                    address="",
                    event_type="安全隐患",
                    urgency="高",
                    scene_tag="",
                    handler="",
                    status="",
                    created_at="",
                    confirmation_required=True,
                    emergency_type=semantic_result.get("emergency_type", ""),
                ),
            )

        # 外部资源场景：语义校验判定为生命急救或紧急救援，且用户未确认时触发弹窗
        scene_tag = semantic_result.get("scene_tag", "")
        if scene_tag in ("生命急救", "紧急救援") and not payload.confirmed:
            # 优先使用接收模块已推断的 emergency_type，避免二次推断与语义判断不一致
            inferred = semantic_result.get("emergency_type")
            if not inferred:
                inferred = dispatch_agent._infer_emergency_type(payload.description)
            if not inferred:
                if scene_tag == "生命急救":
                    inferred = "medical"
                else:
                    # 紧急救援不默认fire，根据描述进一步区分
                    desc = payload.description
                    if re.search(r"火灾|起火|着火|燃气泄漏|煤气泄漏|爆炸|坍塌|电梯困人|高空坠物", desc):
                        inferred = "fire"
                    else:
                        inferred = "police"
            return EventResponse(
                success=True,
                error=f"检测到高风险描述「{payload.description.strip()}」，请确认是否向外部急救资源求助",
                data=EventResponseData(
                    event_id="",
                    address="",
                    event_type=event_type,
                    urgency=semantic_result.get("urgency", "高"),
                    scene_tag="",
                    handler="",
                    status="",
                    created_at="",
                    confirmation_required=True,
                    emergency_type=inferred,
                ),
            )

        if event_type == "无效输入":
            logger.warning("语义校验拦截：description='%s'", redact_pii(payload.description))
            return EventResponse(
                success=False,
                error="输入内容无效（如纯问候、闲聊或无实质内容的描述），请提供具体的社区事务描述",
            )

        if event_type == "API异常":
            logger.error("语义校验API异常：description='%s'", redact_pii(payload.description))
            # API异常时fallback到待审核，不丢弃消息
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=payload.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    user=current_user,
                    error="语义校验服务异常，已转人工审核",
                    lat=payload.lat,
                    lng=payload.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            api_err_state = {
                "description": payload.description,
                "address": "",
                "event_type": "待审核",
                "urgency": "中",
                "scene_tag": "常规",
                "handler": "",
                "confidence": "none",
                "confirmation_required": False,
                "emergency_type": "人工部",
                "confirmed": False,
                "status": "待审核",
            }
            bg_task = asyncio.create_task(
                _process_event(event_id, api_err_state, current_user["id"], payload.lat, payload.lng)
            )
            _background_tasks.add(bg_task)
            bg_task.add_done_callback(_background_tasks.discard)
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
            # 置信度低或地址缺失，创建待审核事件，派给人工部处理
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=payload.description,
                    created_at=created_at,
                    status="待审核",
                    address=semantic_result.get("address", ""),
                    event_type="待审核",
                    urgency=semantic_result.get("urgency", "中"),
                    scene_tag=semantic_result.get("scene_tag", "常规"),
                    user=current_user,
                    lat=payload.lat,
                    lng=payload.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台异步任务，让 dispatch_agent 分配 handler="人工部" 并记录
            semantic_result["status"] = "待审核"
            bg_task = asyncio.create_task(
                _process_event(event_id, semantic_result, current_user["id"], payload.lat, payload.lng)
            )
            _background_tasks.add(bg_task)
            bg_task.add_done_callback(_background_tasks.discard)
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
            _tasks[event_id] = _build_task(
                event_id=event_id,
                description=payload.description,
                created_at=created_at,
                status="处理中",
                address=semantic_result.get("address", ""),
                event_type=semantic_result.get("event_type", ""),
                urgency=semantic_result.get("urgency", ""),
                scene_tag=semantic_result.get("scene_tag", ""),
                user=current_user,
                lat=payload.lat,
                lng=payload.lng,
                beneficiary=beneficiary,
            )
            _save_tasks(_tasks)

        # 启动后台异步任务，传入已校验结果，避免二次调用 LLM API
        bg_task = asyncio.create_task(
            _process_event(event_id, semantic_result, current_user["id"], payload.lat, payload.lng)
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
        hard = _check_hard_rules_first(payload.description)
        if hard is not None:
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=payload.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="高",
                    scene_tag=hard["scene_tag"],
                    user=current_user,
                    error=f"处理异常已转人工：{type(exc).__name__}",
                    lat=payload.lat,
                    lng=payload.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            hard_state = {
                "description": payload.description,
                "address": "",
                "event_type": "待审核",
                "urgency": "高",
                "scene_tag": hard["scene_tag"],
                "handler": "",
                "confidence": "none",
                "confirmation_required": False,
                "emergency_type": "人工部",
                "confirmed": False,
                "status": "待审核",
            }
            bg_task = asyncio.create_task(
                _process_event(event_id, hard_state, current_user["id"], payload.lat, payload.lng)
            )
            _background_tasks.add(bg_task)
            bg_task.add_done_callback(_background_tasks.discard)
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
        reply=task.get("reply") or None,
    )


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/cancel
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/cancel")
async def cancel_event(
    event_id: str,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    """
    居民撤销自己提交的事件（提交后 5 分钟内，任何状态均可撤销，「已撤销」除外）。

    仅事件提交者本人可撤销（管理员即使调用也返回 403，不支持代撤销）。
    5 分钟窗口以后端为权威：now - created_at > 300 秒即拒绝；
    created_at 解析失败按超时处理。撤销仅标记状态为「已撤销」，保留全部记录字段。
    """
    async with _task_lock:
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        # 归属校验：仅本人可撤销（非本人，含管理员代撤销 -> 403）
        if task.get("user_id") != current_user.get("id"):
            raise HTTPException(status_code=403, detail="无权操作该事件")
        if task.get("status") == "已撤销":
            raise HTTPException(status_code=400, detail="事件已撤销")
        # 5 分钟窗口：created_at 按 "%Y-%m-%d %H:%M:%S" 解析，解析失败按超时处理
        try:
            created = datetime.strptime(task.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            created = None
        if created is None or (datetime.now() - created).total_seconds() > 300:
            raise HTTPException(status_code=400, detail="已超过5分钟，无法撤销")
        task["status"] = "已撤销"
        _save_tasks(_tasks)

    return {
        "success": True,
        "data": {
            "event_id": task["event_id"],
            "status": task["status"],
        },
    }


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/accept
# ------------------------------------------------------------------
class AcceptRequest(BaseModel):
    reply: str = Field(default="", description="受理时填写的回复内容（可选）")


# 可转交的处理部门白名单（与 dispatch_agent.EVENT_TYPE_TO_HANDLER 保持一致）
VALID_HANDLERS = ("物业部", "环卫部", "安保部", "调解员", "工程部", "综合部")


@app.post("/api/events/{event_id}/accept")
async def accept_event(
    event_id: str,
    request: AcceptRequest | None = None,
    current_user: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    后台人员受理待审核事件，将状态更新为"已受理"。
    可选携带 reply：受理时一并填写回复内容。
    """
    async with _task_lock:
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        if task.get("status") != "待审核":
            raise HTTPException(status_code=400, detail="仅待审核事件可受理")
        task["status"] = "已受理"
        task["reviewer_id"] = current_user.get("id", "")
        if request and (request.reply or "").strip():
            reply_entry = {
                "content": request.reply.strip(),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "reviewer_id": current_user.get("id", ""),
                "reviewer_name": current_user.get("real_name", ""),
            }
            task.setdefault("replies", []).append(reply_entry)
            task["reply"] = request.reply.strip()
            task["completed_at"] = reply_entry["created_at"]
        _save_tasks(_tasks)

    # 追加记录到 events.jsonl
    try:
        record_agent.record_node({
            "description": task["description"],
            "address": task.get("address", ""),
            "event_type": task.get("event_type", ""),
            "urgency": task.get("urgency", ""),
            "scene_tag": task.get("scene_tag", ""),
            "handler": task.get("handler", ""),
            "status": "已受理",
            "created_at": "",
            "user_id": task.get("user_id", ""),
            "confidence": task.get("confidence", ""),
            "reply": "",
        })
    except Exception as exc:
        logger.warning("受理记录写入失败：event_id=%s，异常=%s", event_id, exc)

    return {
        "success": True,
        "data": {
            "event_id": task["event_id"],
            "status": task["status"],
            "handler": task.get("handler", ""),
        },
    }


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/reply
# ------------------------------------------------------------------
class ReplyRequest(BaseModel):
    reply: str = Field(..., min_length=1, description="后台回复内容")


class TransferRequest(BaseModel):
    handler: str = Field(..., min_length=1, description="转交的处理部门")
    reply: str = Field(..., min_length=1, description="转交说明（必填）")


@app.post("/api/events/{event_id}/reject")
async def reject_event(
    event_id: str,
    request: ReplyRequest,
    current_user: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    管理员拒绝事件（必须写回复），状态改为"已撤销"。
    用户端与管理员后台的事件列表均显示为已撤销。
    """
    async with _task_lock:
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        if task.get("status") in ("已撤销", "拒绝"):
            raise HTTPException(status_code=400, detail="事件已撤销或已拒绝")
        if task.get("status") not in ("待审核", "已受理", "已完成"):
            raise HTTPException(status_code=400, detail="当前状态不可拒绝")
        task["status"] = "拒绝"
        task["reviewer_id"] = current_user.get("id", "")
        reply_entry = {
            "content": f"【已拒绝】{request.reply}",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reviewer_id": current_user.get("id", ""),
            "reviewer_name": current_user.get("real_name", ""),
        }
        task.setdefault("replies", []).append(reply_entry)
        task["reply"] = request.reply
        task["completed_at"] = reply_entry["created_at"]
        _save_tasks(_tasks)

    try:
        record_agent.record_node({
            "description": task["description"],
            "address": task.get("address", ""),
            "event_type": task.get("event_type", ""),
            "urgency": task.get("urgency", ""),
            "scene_tag": task.get("scene_tag", ""),
            "handler": task.get("handler", ""),
            "status": "拒绝",
            "created_at": "",
            "user_id": task.get("user_id", ""),
            "confidence": task.get("confidence", ""),
            "reply": request.reply,
        })
    except Exception as exc:
        logger.warning("拒绝记录写入失败：event_id=%s，异常=%s", event_id, exc)

    return {
        "success": True,
        "data": {
            "event_id": task["event_id"],
            "status": task["status"],
        },
    }


@app.post("/api/events/{event_id}/transfer")
async def transfer_event(
    event_id: str,
    request: TransferRequest,
    current_user: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    管理员将事件转交到指定部门（必须写回复）。
    转交后事件处理部门更新为转交部门；用户端与管理端列表同步显示新部门。
    """
    handler = request.handler.strip()
    if handler not in VALID_HANDLERS:
        raise HTTPException(status_code=400, detail=f"无效的转交部门：{handler}")
    async with _task_lock:
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        if task.get("status") == "已撤销":
            raise HTTPException(status_code=400, detail="事件已撤销，无法转交")
        if task.get("status") not in ("待审核", "已受理", "已完成"):
            raise HTTPException(status_code=400, detail="当前状态不可转交")
        task["handler"] = handler
        if task.get("status") == "待审核":
            task["status"] = "已受理"
        task["reviewer_id"] = current_user.get("id", "")
        reply_entry = {
            "content": f"【转交至{handler}】{request.reply}",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reviewer_id": current_user.get("id", ""),
            "reviewer_name": current_user.get("real_name", ""),
        }
        task.setdefault("replies", []).append(reply_entry)
        task["reply"] = request.reply
        task["completed_at"] = reply_entry["created_at"]
        _save_tasks(_tasks)

    try:
        record_agent.record_node({
            "description": task["description"],
            "address": task.get("address", ""),
            "event_type": task.get("event_type", ""),
            "urgency": task.get("urgency", ""),
            "scene_tag": task.get("scene_tag", ""),
            "handler": handler,
            "status": task["status"],
            "created_at": "",
            "user_id": task.get("user_id", ""),
            "confidence": task.get("confidence", ""),
            "reply": request.reply,
        })
    except Exception as exc:
        logger.warning("转交记录写入失败：event_id=%s，异常=%s", event_id, exc)

    return {
        "success": True,
        "data": {
            "event_id": task["event_id"],
            "status": task["status"],
            "handler": handler,
        },
    }


@app.post("/api/events/{event_id}/reply")
async def reply_event(
    event_id: str,
    request: ReplyRequest,
    current_user: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    后台人员提交回复，将状态更新为"已完成"。
    """
    async with _task_lock:
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        if task.get("status") not in ("已受理", "待审核", "已完成"):
            raise HTTPException(status_code=400, detail="仅已受理、待审核或已完成事件可提交回复")
        # 首次回复时才将状态设为已完成；如果已经是已完成，保持不动
        if task.get("status") != "已完成":
            task["status"] = "已完成"
        reply_entry = {
            "content": request.reply,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "reviewer_id": current_user.get("id", ""),
            "reviewer_name": current_user.get("real_name", ""),
        }
        task.setdefault("replies", []).append(reply_entry)
        task["reply"] = request.reply
        task["reviewer_id"] = current_user.get("id", "")
        task["completed_at"] = reply_entry["created_at"]
        _save_tasks(_tasks)

    # 追加记录到 events.jsonl
    try:
        record_agent.record_node({
            "description": task["description"],
            "address": task.get("address", ""),
            "event_type": task.get("event_type", ""),
            "urgency": task.get("urgency", ""),
            "scene_tag": task.get("scene_tag", ""),
            "handler": task.get("handler", ""),
            "status": "已完成",
            "created_at": "",
            "user_id": task.get("user_id", ""),
            "confidence": task.get("confidence", ""),
            "reply": request.reply,
        })
    except Exception as exc:
        logger.warning("回复记录写入失败：event_id=%s，异常=%s", event_id, exc)

    return {
        "success": True,
        "data": {
            "event_id": task["event_id"],
            "status": task["status"],
            "reply": task["reply"],
        },
    }


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/mark_read
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/mark_read")
async def mark_event_read(
    event_id: str,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    """
    用户查看回复后标记为已读。
    """
    async with _task_lock:
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        if current_user.get("role") != "admin" and task.get("user_id") != current_user.get("id"):
            raise HTTPException(status_code=403, detail="无权访问此事件")
        task["user_read_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_tasks(_tasks)
    return {"success": True}


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
