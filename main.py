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
import copy
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Depends, Header, Request, Body
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, model_validator
from prometheus_fastapi_instrumentator import Instrumentator, metrics
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# 复用已有工作流与持久化配置
from workflow import workflow, WorkflowState, dispatch_record_workflow
import record_agent
from receive_agent import receive_node, _check_hard_rules_first, _check_fuzzy_emergency
import receive_agent  # noqa: F811  用于调试：确认加载的模块路径
import dispatch_agent
import auth
import geo
import community_store
from secure_store import encrypt_field, decrypt_field

logger = logging.getLogger("main")
def _compute_handler(event_type: str, urgency: str, scene_tag: str, emergency_type: str = "") -> str:
    """同步计算处理部门（与后台 dispatch_node 保持一致），避免提交响应与实际派单结果不一致。"""
    return dispatch_agent.dispatch_node({
        "description": "",
        "address": "",
        "event_type": event_type or "",
        "urgency": urgency or "",
        "scene_tag": scene_tag or "",
        "handler": "",
        "emergency_type": emergency_type or "",
    }).get("handler", "")



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


# ------------------------------------------------------------------
# 任务字段级加解密 helper
# ------------------------------------------------------------------
_TASK_SENSITIVE_FIELDS = {
    "description", "address", "user_name", "user_phone", "user_id_card",
    "reply", "beneficiary_name", "beneficiary_phone",
    "beneficiary_building", "beneficiary_unit", "beneficiary_room",
    "user_building", "user_unit", "user_room",
}


def _encrypt_task_fields(task: dict) -> dict:
    """加密任务中的敏感字段，返回副本（不修改原 dict）。"""
    t = copy.deepcopy(task)
    for fld in _TASK_SENSITIVE_FIELDS:
        if fld in t and t[fld]:
            t[fld] = encrypt_field(t[fld])
    if "replies" in t and isinstance(t["replies"], list):
        for r in t["replies"]:
            if isinstance(r, dict) and r.get("content"):
                r["content"] = encrypt_field(r["content"])
    return t


def _decrypt_task_fields(task: dict) -> dict:
    """解密任务中的敏感字段，原地修改。"""
    for fld in _TASK_SENSITIVE_FIELDS:
        if fld in task:
            task[fld] = decrypt_field(task[fld])
    if "replies" in task and isinstance(task["replies"], list):
        for r in task["replies"]:
            if isinstance(r, dict):
                r["content"] = decrypt_field(r.get("content"))
    return task


def _load_tasks() -> dict[str, dict[str, Any]]:
    """
    从磁盘加载任务状态。文件不存在或损坏时返回空字典。
    敏感字段自动透明解密。
    """
    if not os.path.exists(TASKS_FILE):
        return {}
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {k: _decrypt_task_fields(v) for k, v in data.items()}
            return {}
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.error("加载任务状态文件失败，将使用空状态。异常=%s", exc)
        return {}


def _save_tasks(tasks: dict[str, dict[str, Any]]) -> None:
    """
    将全量任务状态写入磁盘。调用方需自行保证并发安全（在外层锁内调用）。
    敏感字段加密后落盘，内存 dict 保持明文。
    使用原子写入（临时文件+os.replace）避免多进程并发导致文件损坏或数据丢失。
    """
    _ensure_data_dir()
    try:
        encrypted_tasks = {k: _encrypt_task_fields(v) for k, v in tasks.items()}
        tmp_path = TASKS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(encrypted_tasks, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, TASKS_FILE)
    except (OSError, TypeError, ValueError) as exc:
        logger.error("持久化任务状态失败，文件='%s'，异常=%s", TASKS_FILE, exc)


def _refresh_tasks() -> None:
    """整体重载任务状态：从文件加载最新全量数据，覆盖内存中的可能过期的缓存。
    用于查询与写操作，确保看到其他进程持久化的最新状态。
    采用原地更新（不重新绑定变量），保留所有外部引用的一致性。
    """
    loaded = _load_tasks()
    _tasks.clear()
    _tasks.update(loaded)


def _reload_tasks() -> None:
    """补充加载任务状态：仅加载内存中不存在的任务，避免覆盖当前进程缓存。
    用于 _process_event 等后台链路，兼容测试直接种入内存的任务。
    """
    loaded = _load_tasks()
    for k, v in loaded.items():
        if k not in _tasks:
            _tasks[k] = v


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
            _reload_tasks()
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
                logger.error("事件处理结果无效（非 dict），event_id=%s，result=%r", event_id, result)
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
                    "emergency_type": task.get("emergency_type", pre_checked_state.get("emergency_type", "")),
                })
                _save_tasks(_tasks)
    except asyncio.TimeoutError:
        async with _task_lock:
            _reload_tasks()
            task = _tasks.get(event_id)
            # 状态守卫：仅处理中/待审核可被改写为处理超时，防止覆盖「已撤销」
            if task is not None and task.get("status") in ("处理中", "待审核"):
                task["status"] = "处理超时"
                task["error"] = "AI 处理超过60秒，已超时"
                _save_tasks(_tasks)
        logger.warning("事件处理超时，event_id=%s", event_id)
    except Exception as exc:
        async with _task_lock:
            _reload_tasks()
            task = _tasks.get(event_id)
            # 状态守卫：仅处理中/待审核可被改写为处理失败，防止覆盖「已撤销」
            if task is not None and task.get("status") in ("处理中", "待审核"):
                task["status"] = "处理失败"
                task["error"] = f"{type(exc).__name__}：{exc}"
                _save_tasks(_tasks)
        logger.error("事件处理失败，event_id=%s，异常=%s", event_id, exc)


# ------------------------------------------------------------------
# 自动受理：待审核事件超过 AUTO_ACCEPT_HOURS 小时未受理自动转为已受理
# ------------------------------------------------------------------
async def _auto_accept_stale_pending() -> int:
    """将超过 AUTO_ACCEPT_HOURS 小时仍未受理的待审核事件自动转为已受理。"""
    if not config.AUTO_ACCEPT_ENABLED:
        return 0
    now = datetime.now()
    cutoff = (now - timedelta(hours=config.AUTO_ACCEPT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    changed = 0
    async with _task_lock:
        _refresh_tasks()
        for task in _tasks.values():
            if task.get("status") != "待审核":
                continue
            created = task.get("created_at", "")
            if not created or created > cutoff:
                continue
            task["status"] = "已受理"
            task["auto_accepted_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            task["auto_accepted"] = True
            changed += 1
        if changed:
            _save_tasks(_tasks)
    if changed:
        logger.info("自动受理 %d 条超时未处理的待审核事件", changed)
    return changed


async def _auto_accept_loop() -> None:
    """后台定时扫描并自动受理超时未处理的待审核事件。"""
    while True:
        try:
            await _auto_accept_stale_pending()
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("自动受理扫描异常：%s", exc)
        await asyncio.sleep(config.AUTO_ACCEPT_CHECK_SECONDS)


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
    emergency_type: str | None = None,
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
        "emergency_type": emergency_type or "",
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


@app.on_event("startup")
async def _auto_accept_startup() -> None:
    """启动后后台循环扫描并自动受理超时未处理的待审核事件。"""
    asyncio.create_task(_auto_accept_loop())


# 注册 CORS 中间件，允许前端跨域调用（白名单取自环境变量，默认含本机与生产前端）
_cors_origins = config.CORS_ALLOW_ORIGINS
_cors_allow_credentials = "*" not in _cors_origins
if "*" in _cors_origins:
    logger.warning(
        "CORS_ALLOW_ORIGINS 包含通配符 '*', allow_credentials 已自动置为 False"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------------
# Prometheus 指标端点
# ------------------------------------------------------------------
Instrumentator().instrument(app).add(metrics.default()).expose(
    app, endpoint="/metrics", include_in_schema=False
)

# ------------------------------------------------------------------
# slowapi 限流器
# ------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, enabled=config.RATE_LIMIT_ENABLED)
app.state.limiter = limiter


async def _custom_rate_limit_handler(request, exc):
    """自定义限流响应格式，与项目统一格式一致。"""
    return JSONResponse(
        status_code=429,
        content={"success": False, "error": "请求过于频繁，请稍后再试"},
    )


app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)


def _rate_limit_key_user(request: Request) -> str:
    """从请求头提取 Bearer Token 对应的 user_id，用于事件提交 per-user 限流。"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        user = auth.get_current_user(token)
        if user:
            return user.get("id", "anonymous")
    return "anonymous"


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
    @model_validator(mode="after")
    def _auto_fill_handler(self) -> "EventResponseData":
        # 提交响应与后台派单保持一致：handler 为空时按 event_type/urgency/scene_tag/emergency_type 同步计算
        if not self.handler:
            self.handler = _compute_handler(
                self.event_type or "",
                self.urgency or "",
                self.scene_tag or "",
                self.emergency_type or "",
            )
        return self


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
    emergency_type: str | None = None
    handler: str | None = None
    created_at: str
    completed_at: str | None = None
    error: str | None = None
    reply: str | None = None


class RejectRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500, description="拒绝理由")
class AcceptRequest(BaseModel):
    reply: str = Field(default="", max_length=5000, description="受理时回复内容（可空）")




class ReplyRequest(BaseModel):
    reply: str = Field(..., min_length=1, max_length=5000, description="后台回复内容")


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
security = HTTPBearer(auto_error=False, description="请输入 Bearer Token")


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def get_current_user_dependency(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    token = credentials.credentials if credentials else None
    user = auth.get_current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    return user


async def get_admin_dependency(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    token = credentials.credentials if credentials else None
    user = auth.get_current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="权限不足，仅管理员可访问")
    return user


# 可选认证（用于兼容场景，未登录也允许但可获取用户信息）
async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any] | None:
    token = credentials.credentials if credentials else None
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
@limiter.limit(config.RATE_LIMIT_LOGIN)
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
    # 注册成功后自动登录，返回 token
    login_ok, login_msg, login_data = auth.login_user(body.username, body.password)
    if login_ok and login_data:
        return AuthResponse(success=True, data=login_data, error="注册成功")
    # 【修改】自动登录失败时返回 success=False，不再返回无 token 的 success=True
    return AuthResponse(success=False, error="注册成功但自动登录失败，请手动登录")


@app.post("/api/auth/login", response_model=AuthResponse)
@limiter.limit(config.RATE_LIMIT_LOGIN)
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
async def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, str]:
    """
    用户登出：使当前 Token 在服务端立即失效。
    """
    token = credentials.credentials if credentials else None
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
        _refresh_tasks()
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
                "emergency_type": task.get("emergency_type", ""),
                "handler": task.get("handler", ""),
                "status": task["status"],
                "created_at": task["created_at"],
                "reply": task.get("reply", ""),
                "error": task.get("error", ""),
                "confidence": task.get("confidence", ""),
                "replies": replies,
                "has_new_reply": has_new_reply,
                "rejected_reason": task.get("rejected_reason", ""),
                "rejected_at": task.get("rejected_at", ""),
                "rejected_by": task.get("rejected_by", ""),
                "withdrawn_at": task.get("withdrawn_at", ""),
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
@app.post("/api/events", response_model=EventResponse)
@limiter.limit(config.RATE_LIMIT_EVENTS, key_func=_rate_limit_key_user)
async def create_event(
    request: Request,
    body: EventRequest,
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
        if body.beneficiary_type not in ("self", "proxy"):
            return EventResponse(
                success=False,
                error="提交方式不合法，仅支持本人（self）或代人办（proxy）",
            )
        if body.beneficiary_type == "proxy":
            missing = []
            for label, val in (
                ("被帮助人姓名", body.beneficiary_name),
                ("手机号", body.beneficiary_phone),
                ("楼栋", body.beneficiary_building),
                ("单元", body.beneficiary_unit),
                ("房间号", body.beneficiary_room),
            ):
                if not (val or "").strip():
                    missing.append(label)
            if missing:
                return EventResponse(
                    success=False,
                    error="代人办需填写：" + "、".join(missing),
                )
        beneficiary = _resolve_beneficiary(body, current_user)
        # ------------------------------------------------------------------
        # 前置硬规则检查（生命安全优先）：命中则跳过所有LLM调用
        # ------------------------------------------------------------------
        hard_rule_result = _check_hard_rules_first(body.description)
        if hard_rule_result is not None:
            if not body.confirmed:
                # 未确认：返回弹窗确认，不创建任务
                return EventResponse(
                    success=True,
                    error=f"检测到高风险描述「{body.description.strip()}」，请确认是否向外部急救资源求助",
                    data=EventResponseData(
                        event_id="",
                        address="",
                        event_type=hard_rule_result["event_type"],
                        urgency=hard_rule_result["urgency"],
                        scene_tag=hard_rule_result["scene_tag"],
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
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=body.description,
                    created_at=created_at,
                    status="处理中",
                    address="",
                    event_type=hard_rule_result["event_type"],
                    urgency=hard_rule_result["urgency"],
                    scene_tag=hard_rule_result["scene_tag"],
                    emergency_type=hard_rule_result.get("emergency_type", ""),
                    user=current_user,
                    lat=body.lat,
                    lng=body.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台异步任务
            bg_task = asyncio.create_task(
                _process_event(event_id, hard_rule_result, current_user["id"], body.lat, body.lng)
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
                    emergency_type=hard_rule_result.get("emergency_type", ""),
                ),
            )

        # ------------------------------------------------------------------
        # 前置模糊急救检查：高风险短词且用户未确认时，返回确认提示，不创建任务
        # ------------------------------------------------------------------
        if not body.confirmed:
            fuzzy_emergency = _check_fuzzy_emergency(body.description)
            if fuzzy_emergency is not None:
                logger.warning(
                    "前置模糊急救命中（%s），返回确认提示：description='%s'",
                    fuzzy_emergency["emergency_type"],
                    body.description,
                )
                return EventResponse(
                    success=True,
                    error=f"检测到高风险关键词「{body.description.strip()}」，请补充具体地址和详细描述后重新提交",
                    data=EventResponseData(
                        event_id="",
                        address="",
                        event_type="安全隐患",
                        urgency="高",
                        scene_tag=(
                            "生命急救"
                            if fuzzy_emergency["emergency_type"] == "medical"
                            else "紧急救援"
                        ),
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
                "description": body.description,
                "address": "",
                "event_type": "",
                "urgency": "",
                "scene_tag": "",
                "handler": "",
                "confidence": "",
                "confirmation_required": False,
                "emergency_type": body.emergency_type or "",
                "confirmed": body.confirmed,
            }
            semantic_result = await asyncio.wait_for(
                asyncio.to_thread(receive_node, check_state),
                timeout=50.0,  # 3轮并行×15秒，留足余量
            )
        except asyncio.TimeoutError:
            logger.warning("语义校验超时，创建待审核事件：description='%s'", body.description)
            # 超时无法判断语义，创建待审核事件转人工部处理
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=body.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    emergency_type="",
                    user=current_user,
                    error="语义校验超时，已转人工审核",
                    lat=body.lat,
                    lng=body.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            timeout_state = {
                "description": body.description,
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
                _process_event(event_id, timeout_state, current_user["id"], body.lat, body.lng)
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
            logger.error("语义校验异常：description='%s'，异常=%s", body.description, exc)
            # 异常时fallback到待审核，不丢弃消息
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=body.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    emergency_type="",
                    user=current_user,
                    error=f"语义校验异常，已转人工审核：{type(exc).__name__}",
                    lat=body.lat,
                    lng=body.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            exc_state = {
                "description": body.description,
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
                _process_event(event_id, exc_state, current_user["id"], body.lat, body.lng)
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
            logger.error("语义校验返回无效结果：description='%s'，result=%r", body.description, semantic_result)
            # 复用「API异常」降级路径：建待审核任务（error="语义校验服务异常，已转人工审核"）、
            # 启动 _process_event（emergency_type="人工部"、status="待审核"）、
            # 返回 EventResponse(success=True, data.status="待审核", error=None)
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=body.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    emergency_type="",
                    user=current_user,
                    error="语义校验服务异常，已转人工审核",
                    lat=body.lat,
                    lng=body.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            invalid_state = {
                "description": body.description,
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
                _process_event(event_id, invalid_state, current_user["id"], body.lat, body.lng)
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
                error=f"检测到高风险关键词「{body.description.strip()}」，请补充具体地址和详细描述后重新提交",
                data=EventResponseData(
                    event_id="",
                    address="",
                    event_type="安全隐患",
                    urgency="高",
                    scene_tag=semantic_result.get("scene_tag", ""),
                    handler="",
                    status="",
                    created_at="",
                    confirmation_required=True,
                    emergency_type=semantic_result.get("emergency_type", ""),
                ),
            )

        # 外部资源场景：语义校验判定为生命急救或紧急救援，且用户未确认时触发弹窗
        scene_tag = semantic_result.get("scene_tag", "")
        if scene_tag in ("生命急救", "紧急救援") and not body.confirmed:
            # 优先使用接收模块已推断的 emergency_type，避免二次推断与语义判断不一致
            inferred = semantic_result.get("emergency_type")
            if not inferred:
                inferred = receive_agent._resolve_emergency_type(body.description, scene_tag)
            if not inferred:
                if scene_tag == "生命急救":
                    inferred = "medical"
                else:
                    # 紧急救援不默认fire，根据描述进一步区分
                    desc = body.description
                    if re.search(r"火灾|起火|着火|燃气泄漏|煤气泄漏|爆炸|坍塌|电梯困人|高空坠物", desc):
                        inferred = "fire"
                    else:
                        inferred = "police"
            return EventResponse(
                success=True,
                error=f"检测到高风险描述「{body.description.strip()}」，请确认是否向外部急救资源求助",
                data=EventResponseData(
                    event_id="",
                    address="",
                    event_type=event_type,
                    urgency=semantic_result.get("urgency", "高"),
                    scene_tag=semantic_result.get("scene_tag", ""),
                    handler="",
                    status="",
                    created_at="",
                    confirmation_required=True,
                    emergency_type=inferred,
                ),
            )

        if event_type == "无效输入":
            logger.warning("语义校验拦截：description='%s'", body.description)
            return EventResponse(
                success=False,
                error="输入内容无效（如纯问候、闲聊或无实质内容的描述），请提供具体的社区事务描述",
            )

        if event_type == "API异常":
            logger.error("语义校验API异常：description='%s'", body.description)
            # API异常时fallback到待审核，不丢弃消息
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=body.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="中",
                    scene_tag="常规",
                    emergency_type="",
                    user=current_user,
                    error="语义校验服务异常，已转人工审核",
                    lat=body.lat,
                    lng=body.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            api_err_state = {
                "description": body.description,
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
                _process_event(event_id, api_err_state, current_user["id"], body.lat, body.lng)
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
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=body.description,
                    created_at=created_at,
                    status="待审核",
                    address=semantic_result.get("address", ""),
                    event_type="待审核",
                    urgency=semantic_result.get("urgency", "中"),
                    scene_tag=semantic_result.get("scene_tag", "常规"),
                    emergency_type=semantic_result.get("emergency_type", ""),
                    user=current_user,
                    lat=body.lat,
                    lng=body.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台异步任务，让 dispatch_agent 分配 handler="人工部" 并记录
            semantic_result["status"] = "待审核"
            bg_task = asyncio.create_task(
                _process_event(event_id, semantic_result, current_user["id"], body.lat, body.lng)
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
            _refresh_tasks()
            _tasks[event_id] = _build_task(
                event_id=event_id,
                description=body.description,
                created_at=created_at,
                status="处理中" if body.confirmed else "待审核",
                address=semantic_result.get("address", ""),
                event_type=semantic_result.get("event_type", ""),
                urgency=semantic_result.get("urgency", ""),
                scene_tag=semantic_result.get("scene_tag", ""),
                emergency_type=semantic_result.get("emergency_type", ""),
                user=current_user,
                lat=body.lat,
                lng=body.lng,
                beneficiary=beneficiary,
            )
            _save_tasks(_tasks)

        # 启动后台异步任务，传入已校验结果，避免二次调用 LLM API
        semantic_result["confirmed"] = body.confirmed
        semantic_result["status"] = "处理中" if body.confirmed else "待审核"
        bg_task = asyncio.create_task(
            _process_event(event_id, semantic_result, current_user["id"], body.lat, body.lng)
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
                status="处理中" if body.confirmed else "待审核",
                created_at=created_at,
                emergency_type=semantic_result.get("emergency_type", ""),
            ),
        )

    except Exception as exc:
        # 最后兜底：生命急救/紧急救援消息绝不丢弃
        hard = _check_hard_rules_first(body.description)
        if hard is not None:
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id,
                    description=body.description,
                    created_at=created_at,
                    status="待审核",
                    address="",
                    event_type="待审核",
                    urgency="高",
                    scene_tag=hard["scene_tag"],
                    emergency_type="",
                    user=current_user,
                    error=f"处理异常已转人工：{type(exc).__name__}",
                    lat=body.lat,
                    lng=body.lng,
                    beneficiary=beneficiary,
                )
                _save_tasks(_tasks)
            # 启动后台让 dispatch_agent 设置 handler="人工部"
            hard_state = {
                "description": body.description,
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
                _process_event(event_id, hard_state, current_user["id"], body.lat, body.lng)
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
        _refresh_tasks()
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
        emergency_type=task.get("emergency_type") or None,
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
        _refresh_tasks()
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
        task["withdrawn_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
@app.post("/api/events/{event_id}/accept")
async def accept_event(
    event_id: str,
    request: AcceptRequest | None = Body(default=None),
    current_user: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    后台人员受理待审核事件，将状态更新为"已受理"。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        if task.get("status") != "待审核":
            raise HTTPException(status_code=400, detail="仅待审核事件可受理")
        task["status"] = "已受理"
        task["reviewer_id"] = current_user.get("id", "")
        if request and request.reply:
            task["reply"] = request.reply
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
# API 端点：POST /api/events/{event_id}/reject
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/reject")
async def reject_event(
    event_id: str,
    request: RejectRequest,
    current_user: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    """
    管理员拒绝待审核事件，将状态更新为"已拒绝"并记录理由。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        if task.get("status") != "待审核":
            raise HTTPException(status_code=400, detail="仅待审核事件可拒绝")
        task["status"] = "已拒绝"
        task["rejected_reason"] = request.reason
        task["reply"] = request.reason
        task["rejected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task["rejected_by"] = current_user.get("id", "")
        _save_tasks(_tasks)

    return {
        "success": True,
        "data": {
            "event_id": task["event_id"],
            "status": task["status"],
            "reason": task["rejected_reason"],
        },
    }


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/reply
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/reply")
async def reply_event(
    event_id: str,
    request: ReplyRequest,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    """
    后台人员或居民提交回复，将状态更新为"已完成"。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")

        # 权限判断：admin 需匹配 reviewer_id；resident 只能回复自己的事件
        is_admin = current_user.get("role") == "admin"
        is_owner = task.get("user_id") == current_user.get("id")
        if is_admin:
            if task.get("reviewer_id") and task.get("reviewer_id") != current_user.get("id"):
                raise HTTPException(status_code=403, detail="仅受理该事件的管理员可回复")
        elif is_owner:
            pass
        else:
            raise HTTPException(status_code=403, detail="无权操作该事件")

        if task.get("status") not in ("已受理", "已完成"):
            raise HTTPException(status_code=400, detail="仅已受理或已完成事件可提交回复")
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
        _refresh_tasks()
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

# ------------------------------------------------------------------
# 全局异常处理：生产环境隐藏内部错误详情（P2-1 修复）
# ------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """捕获所有未处理异常，避免堆栈跟踪泄露到客户端（P2-1）。"""
    import traceback
    logger.error("未捕获异常: %s\n%s", exc, traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "服务器内部错误，请稍后重试"}
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    """统一HTTP异常响应格式（P2-1）。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": exc.detail}
    )
