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

from fastapi import FastAPI, HTTPException, Depends, Header, Request, Body, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response
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
import db
import weekly_report
import media_store
import asr
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


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _timeline_append(task: dict, type_: str, text: str, actor: str = "", photos: list | None = None) -> dict:
    """向事件追加一条时间线节点（文本不存敏感字段）。"""
    node = {"type": type_, "text": text, "time": _now(), "actor": actor or "", "photos": photos or []}
    task.setdefault("timeline", []).append(node)
    return node


def _append_reply(task: dict, content: str, user: dict[str, Any], photos: list | None = None) -> dict:
    """追加一条多轮回复记录（content 走字段级加密，photos 只存 media_id）。"""
    entry = {
        "content": content,
        "created_at": _now(),
        "role": user.get("role", ""),
        "reviewer_id": user.get("id", ""),
        "reviewer_name": user.get("real_name", "") or user.get("username", ""),
        "photos": photos or [],
    }
    task.setdefault("replies", []).append(entry)
    task["reply"] = content
    return entry


def _apply_dispatch(task: dict, event_type: str, urgency: str, scene_tag: str, emergency_type: str = "") -> tuple[str, str, str]:
    """计算并写入 handler / assigned_dept / department_name，返回 (handler, dept_key, dept_name)。"""
    handler = _compute_handler(event_type or "", urgency or "", scene_tag or "", emergency_type or "")
    key, name = dispatch_agent.handler_to_department(handler)
    task["handler"] = handler
    task["assigned_dept"] = key
    task["department_name"] = name
    return handler, key, name



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
                "role": "admin",
                "photos": [],
            })
    for fld in ("user_read_at", "assigned_dept", "department_name", "reviewer_dept", "dept_read_at"):
        if fld not in task:
            task[fld] = ""
    if "audio_transcript" not in task:
        task["audio_transcript"] = ""
    if "media" not in task:
        task["media"] = []
    if "timeline" not in task:
        task["timeline"] = [{
            "type": "提交",
            "text": "居民提交事件",
            "time": task.get("created_at", ""),
            "actor": task.get("user_name", ""),
            "photos": [],
        }]

# 并发锁：保护内存状态更新与文件写入
_task_lock = asyncio.Lock()

# 持有后台任务引用，防止被垃圾回收并避免 "never retrieved" 警告
_background_tasks: set[asyncio.Task] = set()


# 关键词→事件类型确定性快路径（仅用于转写后的语音，命中即派单）
_AUDIO_KEYWORD_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("安全隐患", ("噪音", "噪声", "陌生人", "门禁", "监控", "打架", "盗窃", "偷", "闯入", "扰民", "大声")),
    ("邻里纠纷", ("矛盾", "纠纷", "邻里", "吵架", "投诉邻居", "占道", "养狗")),
    ("公共设施", ("电梯", "路灯", "电路", "停电", "停水", "门锁", "井盖", "健身器材", "设备")),
    ("物业维修", ("漏水", "下水道", "管道", "堵", "水管", "渗水", "外墙", "屋顶", "天花板", "水龙头", "马桶")),
    ("环境卫生", ("垃圾", "清扫", "臭味", "异味", "卫生", "保洁", "老鼠", "蟑螂", "烟头")),
]

def _keyword_dispatch(text: str) -> dict | None:
    """关键词规则：命中常见社区诉求则返回 {event_type, urgency, scene_tag, confidence, emergency_type, address}。"""
    if not text:
        return None
    for event_type, words in _AUDIO_KEYWORD_RULES:
        if any(w in text for w in words):
            return {
                "event_type": event_type,
                "urgency": "中",
                "scene_tag": "常规",
                "confidence": "high",
                "emergency_type": "",
                "address": "",
            }
    return None


async def _process_audio_event(
    event_id: str,
    pre_checked_state: dict[str, str],
    user_id: str,
    lat: float | None,
    lng: float | None,
    audio_ids: list[str],
) -> None:
    """录音事件后台流程：ASR 转写 -> DeepSeek 分类 -> 派单或保持待审核。"""
    transcript = ""
    try:
        audio_id = audio_ids[0]
        data = await asyncio.to_thread(media_store.read_upload, audio_id)
        if data:
            transcript = await asyncio.to_thread(asr.transcribe, data, media_store.ext_of(audio_id))
    except Exception as exc:
        logger.warning("录音转写失败：event_id=%s，异常=%s", event_id, exc)
        transcript = ""

    async with _task_lock:
        _reload_tasks()
        task = _tasks.get(event_id)
        if task is None:
            return
        task["audio_transcript"] = transcript
        if not transcript:
            task["status"] = "待审核"
            _timeline_append(task, "待审核", "录音转写失败，已转人工审核", "系统")
            _save_tasks(_tasks)
            return

    combined = (transcript + " " + (pre_checked_state.get("description", "") or "")).strip() or transcript

    # 快路径1：应急硬规则（最高优先级，跳过 LLM）
    hard = None
    try:
        hard = _check_hard_rules_first(combined)
    except Exception as exc:
        logger.warning("录音事件硬规则检查异常：event_id=%s，异常=%s", event_id, exc)

    # 快路径2：关键词→部门确定性派单
    kw = _keyword_dispatch(combined)

    semantic = None
    used_fast = False
    if hard is not None:
        semantic = hard
        used_fast = True
        logger.info("录音事件应急硬规则命中，跳过LLM：event_id=%s", event_id)
    elif kw is not None:
        semantic = kw
        used_fast = True
        logger.info("录音事件关键词快路径命中：event_id=%s, type=%s", event_id, kw.get("event_type"))
    else:
        try:
            check_state = {
                "description": combined, "address": "", "event_type": "", "urgency": "",
                "scene_tag": "", "handler": "", "confidence": "", "confirmation_required": False,
                "emergency_type": "", "confirmed": False,
            }
            semantic = await asyncio.wait_for(asyncio.to_thread(receive_node, check_state), timeout=50.0)
        except Exception as exc:
            logger.warning("录音事件语义校验异常：event_id=%s，异常=%s", event_id, exc)
            semantic = None

    async with _task_lock:
        _reload_tasks()
        task = _tasks.get(event_id)
        if task is None:
            return
        if not isinstance(semantic, dict) or semantic.get("event_type", "") in ("无效输入", "API异常", "待审核") or (not used_fast and semantic.get("confidence", "") != "high"):
            task["status"] = "待审核"
            _timeline_append(task, "待审核", "录音内容待人工确认归类", "系统")
            _save_tasks(_tasks)
            return
        dispatch_state = {
            "description": combined, "address": semantic.get("address", ""),
            "event_type": semantic.get("event_type", ""), "urgency": semantic.get("urgency", ""),
            "scene_tag": semantic.get("scene_tag", ""), "handler": "", "confidence": semantic.get("confidence", ""),
            "emergency_type": semantic.get("emergency_type", ""),
        }
        result = await asyncio.to_thread(dispatch_agent.dispatch_node, dispatch_state)
        handler = result.get("handler", "")
        key, name = dispatch_agent.handler_to_department(handler)
        task.update({
            "address": semantic.get("address", ""),
            "event_type": semantic.get("event_type", ""),
            "urgency": semantic.get("urgency", ""),
            "scene_tag": semantic.get("scene_tag", ""),
            "handler": handler,
            "assigned_dept": key,
            "department_name": name,
        })
        if key:
            task["status"] = "待处理"
            _timeline_append(task, "待处理", "已派单至" + name, "系统")
        else:
            task["status"] = "待审核"
            _timeline_append(task, "待审核", "已转入超管/人工审核", "系统")
        _save_tasks(_tasks)


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
    含录音的事件改走 _process_audio_event（ASR 转写 + DeepSeek 分类）。
    """

    async with _task_lock:
        _reload_tasks()
        _cur = _tasks.get(event_id)
    _audio_ids = []
    if _cur:
        _audio_ids = [m.get("media_id", "") for m in _cur.get("media", []) if media_store.is_audio(m.get("media_id", ""))]
    if _audio_ids:
        await _process_audio_event(event_id, pre_checked_state, user_id, lat, lng, _audio_ids)
        return

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
                # 新流程：AI 派单成功后进入部门「待处理」；外部资源/人工部进入「待审核」（超管闭环）
                handler = result.get("handler", "")
                key, name = dispatch_agent.handler_to_department(handler)
                task.update({
                    "address": result["address"],
                    "event_type": result["event_type"],
                    "urgency": result["urgency"],
                    "scene_tag": result["scene_tag"],
                    "handler": handler,
                    "assigned_dept": key,
                    "department_name": name,
                    "emergency_type": task.get("emergency_type", pre_checked_state.get("emergency_type", "")),
                })
                if key:
                    if task.get("status") in ("处理中", "待审核"):
                        task["status"] = "待处理"
                        _timeline_append(task, "待处理", "已派单至" + name, "系统")
                    elif task.get("status") == "待处理" and not any(n.get("type") == "待处理" for n in task.get("timeline", [])):
                        _timeline_append(task, "待处理", "已派单至" + name, "系统")
                else:
                    if task.get("status") in ("处理中", "待审核"):
                        task["status"] = "待审核"
                        _timeline_append(task, "待审核", "已转入超管/人工审核", "系统")
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


async def _cleanup_old_recordings() -> int:
    """删除超过保留期的录音，并移除事件内的过期音频引用。返回删除数量。"""
    cutoff_days = config.RECORDING_RETENTION_DAYS
    if cutoff_days <= 0:
        return 0
    now = datetime.now()
    cutoff = now - timedelta(days=cutoff_days)
    expired_ids: set[str] = set()
    try:
        items = await asyncio.to_thread(media_store.list_audio_keys)
    except Exception as exc:
        logger.warning("读取录音列表失败：%s", exc)
        return 0
    for it in items:
        key = (it.get("key") or "")
        lm = it.get("last_modified")
        if not key or lm is None:
            continue
        media_id = key.rsplit("/", 1)[-1]
        try:
            if isinstance(lm, datetime):
                lm_dt = lm
            elif isinstance(lm, str):
                lm_dt = datetime.strptime(lm, "%Y-%m-%d %H:%M:%S")
            elif hasattr(lm, "timestamp"):
                lm_dt = datetime.fromtimestamp(lm.timestamp())
            else:
                continue
        except Exception:
            continue
        if lm_dt < cutoff:
            expired_ids.add(media_id)

    if not expired_ids:
        return 0

    deleted = 0
    async with _task_lock:
        _reload_tasks()
        for mid in expired_ids:
            for task in _tasks.values():
                media = task.get("media") or []
                new_media = [m for m in media if m.get("media_id") != mid]
                if len(new_media) != len(media):
                    task["media"] = new_media
        for mid in expired_ids:
            try:
                if await asyncio.to_thread(media_store.delete_audio, mid):
                    deleted += 1
            except Exception as exc:
                logger.warning("删除录音失败：%s，%s", mid, exc)
        if deleted:
            _save_tasks(_tasks)
    if deleted:
        logger.info("录音清理完成，删除 %d 条超过 %d 天", deleted, cutoff_days)
    return deleted


async def _cleanup_recordings_loop() -> None:
    """后台定时清理超过保留期的录音。"""
    while True:
        try:
            await _cleanup_old_recordings()
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("录音清理扫描异常：%s", exc)
        await asyncio.sleep(config.RECORDING_CLEANUP_CHECK_SECONDS)


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
        "assigned_dept": "",
        "department_name": "",
        "audio_transcript": "",
        "reviewer_id": "",
        "reviewer_dept": "",
        "timeline": [{
            "type": "提交",
            "text": "居民提交事件",
            "time": created_at,
            "actor": user.get("real_name", ""),
            "photos": [],
        }],
        "media": [],
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




async def _maybe_auto_generate_weekly_report() -> bool:
    """每周一凌晨 1 点自动生成并归档本周周报（若当周尚未生成）。"""
    now = datetime.now()
    if now.weekday() != 0 or now.hour != 1:
        return False
    try:
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        week_key = monday.strftime("%Y-%m-%d")
        if weekly_report.load(week_key) is not None:
            return False
        await _build_weekly_report()
        logger.info("已自动生成并归档本周周报：%s", week_key)
        return True
    except Exception as exc:
        logger.warning("自动生成周报失败：%s", exc)
        return False


async def _weekly_report_auto_loop() -> None:
    """后台定时检测：每周一凌晨自动生成全部门周报并入库存档。"""
    while True:
        try:
            await _maybe_auto_generate_weekly_report()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("\u5468\u62a5\u81ea\u52a8\u751f\u6210\u626b\u63cf\u5f02\u5e38\uff1a%s", exc)
        await asyncio.sleep(3600)

@app.on_event("startup")
async def _auto_accept_startup() -> None:
    db.init_db()
    """启动后后台循环扫描并自动受理超时未处理的待审核事件，并定时清理超期录音。"""
    asyncio.create_task(_auto_accept_loop())
    asyncio.create_task(_cleanup_recordings_loop())
    asyncio.create_task(_weekly_report_auto_loop())


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
    description: str = Field(default="", description="居民事件描述字符串（可空，与录音至少其一）", max_length=500)
    confirmed: bool = Field(default=False, description="用户是否已确认高风险描述（用于模糊急救二次提交）")
    emergency_type: str | None = Field(default=None, description="模糊急救类型：medical/police/fire（用于二次提交时传递）")
    lat: float | None = Field(default=None, ge=-90, le=90, description="事件实时定位纬度")
    lng: float | None = Field(default=None, ge=-180, le=180, description="事件实时定位经度")
    building: str | None = Field(default=None, max_length=20, description="事件楼栋（可空，默认取注册住址）")
    unit: str | None = Field(default=None, max_length=20, description="事件单元")
    room: str | None = Field(default=None, max_length=20, description="事件房间号")
    media_ids: list[str] = Field(default_factory=list, description="已上传媒体ID列表（照片/录音）")
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


class WorkHoursRequest(BaseModel):
    work_hours_start: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="上班开始时间，如 09:00")
    work_hours_end: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="上班结束时间，如 18:00")


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
    photos: list[str] = Field(default_factory=list, description="回复附带照片 media_id 列表")


class CompleteRequest(BaseModel):
    reply: str = Field(default="", max_length=5000, description="完成说明（可空）")
    photos: list[str] = Field(default_factory=list, description="留证照片 media_id 列表")


class TypeUpdateRequest(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=50, description="修正后的事件类型")
    urgency: str = Field(default="", max_length=10, description="修正后的紧急程度（可空，不改则不传）")


class DeptUpdateRequest(BaseModel):
    department: str = Field(..., min_length=1, max_length=30, description="目标部门键")


class DeptUserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=6, max_length=64)
    real_name: str = Field(..., min_length=1, max_length=20)
    phone: str = Field(..., pattern=r"^1[3-9]\d{9}$")
    department: str = Field(..., min_length=1, max_length=30)


class DeptUserUpdateRequest(BaseModel):
    username: str | None = Field(default=None, description="用户名，留空表示不修改")
    real_name: str | None = Field(default=None, description="姓名，留空表示不修改")
    phone: str | None = Field(default=None, description="手机号，留空表示不修改")
    password: str | None = Field(default=None, description="密码，留空表示不修改")
    department: str | None = Field(default=None, description="部门字段，留空表示不修改")
    status: str | None = Field(default=None, description="active=启用 / disabled=停用")


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
    department: str = ""
    department_name: str = ""
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


async def get_staff_dependency(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any]:
    """管理员/部门账号均可访问（用于事件处理类操作）。"""
    token = credentials.credentials if credentials else None
    user = auth.get_current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    if user.get("role") not in ("admin", "dept"):
        raise HTTPException(status_code=403, detail="权限不足，仅工作人员可访问")
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
# API 端点：PUT /api/admin/workhours（全局上班时段，仅超管）
# ------------------------------------------------------------------
@app.put("/api/admin/workhours")
async def admin_update_workhours(
    request: WorkHoursRequest,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    try:
        return community_store.save_workhours(request.work_hours_start, request.work_hours_end)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ------------------------------------------------------------------
# API 端点：GET /api/metrics（工作人员）—— 平均响应/处理时长
# ------------------------------------------------------------------
def _parse_dt(s: str) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _diff_min(a: datetime, b: datetime) -> float | None:
    if not a or not b:
        return None
    return (b - a).total_seconds() / 60.0


async def _compute_daily_handling() -> dict[str, Any]:
    whs = community_store.get_workhours()
    today = datetime.now().strftime("%Y-%m-%d")
    dept_values: dict[str, list[float]] = {}
    async with _task_lock:
        _refresh_tasks()
        for task in _tasks.values():
            created = task.get("created_at", "")
            if not isinstance(created, str) or not created.startswith(today):
                continue
            if task.get("status") != "已完成" or not task.get("completed_at"):
                continue
            ad = task.get("assigned_dept", "")
            if not ad or ad not in dispatch_agent.DEPARTMENTS:
                continue
            # 仅统计由部门账号实际完成的事件，超管自己完成的不纳入
            rv_dept = task.get("reviewer_dept", "")
            if not rv_dept or rv_dept not in dispatch_agent.DEPARTMENTS:
                continue
            # 最早的「待处理」时间线 = 派单到部门时间
            first_handle = None
            for n in (task.get("timeline") or []):
                if n.get("type") == "待处理" and n.get("time"):
                    t = _parse_dt(n["time"])
                    if t is not None:
                        first_handle = t
                        break
            if first_handle is None:
                continue
            cplt = _parse_dt(task["completed_at"])
            hmin = _work_minutes_between(first_handle, cplt, whs)
            if hmin is None or hmin < 0:
                continue
            dept_values.setdefault(ad, []).append(hmin)

    dept_results: dict[str, Any] = {}
    for k in dispatch_agent.DEPARTMENTS:
        vals = dept_values.get(k)
        dept_results[k] = round(sum(vals) / len(vals), 1) if vals else None
    all_vals = [v for v in dept_results.values() if v is not None]
    overall = round(sum(all_vals) / len(all_vals), 1) if all_vals else None
    return {
        "work_hours_start": whs.get("work_hours_start", "09:00"),
        "work_hours_end": whs.get("work_hours_end", "18:00"),
        "date": today,
        "dept_results": dept_results,
        "avg_handling_min": overall,
    }


@app.get("/api/metrics")
async def get_metrics(
    current_user: dict[str, Any] = Depends(get_staff_dependency),
) -> dict[str, Any]:
    data = await _compute_daily_handling()
    role = current_user.get("role")
    department = current_user.get("department", "")
    if role == "dept":
        return {
            "role": "dept",
            "dept": department,
            "dept_name": dispatch_agent.DEPARTMENTS.get(department, ""),
            "avg_handling_min": data["dept_results"].get(department),
            "work_hours_start": data["work_hours_start"],
            "work_hours_end": data["work_hours_end"],
            "date": data["date"],
        }
    return {
        "role": "admin",
        "dept_results": data["dept_results"],
        "avg_handling_min": data["avg_handling_min"],
        "work_hours_start": data["work_hours_start"],
        "work_hours_end": data["work_hours_end"],
        "date": data["date"],
    }


# ------------------------------------------------------------------
# AI 周报：本周统计 + AI 逐事件总结 + 每周历史（仅超管）
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# \u5468\u62a5\u6307\u6807\u5de5\u5177\u51fd\u6570\uff08\u4e0a\u73ed\u65f6\u6bb5\u5207\u7247\uff09
# ------------------------------------------------------------------
def _work_minutes_between(start: datetime, end: datetime, whs: dict) -> float:
    """\u8ba1\u7b97 [start, end] \u843d\u5728\u6bcf\u5929\u4e0a\u73ed\u65f6\u6bb5\uff08\u5468\u4e00~\u5468\u4e94\uff0cwhs.work_hours_start~end\uff09\u5185\u7684\u5206\u949f\u6570\u3002"""
    if not start or not end or end <= start:
        return 0.0
    try:
        ws_h, ws_m = map(int, whs.get("work_hours_start", "09:00").split(":"))
        we_h, we_m = map(int, whs.get("work_hours_end", "18:00").split(":"))
    except Exception:
        ws_h, ws_m, we_h, we_m = 9, 0, 18, 0
    ws = ws_h * 60 + ws_m
    we = we_h * 60 + we_m
    total = 0.0
    cur = start
    while cur < end:
        if cur.weekday() >= 5:  # \u5468\u672b
            cur = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            continue
        day_start = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        win_start = day_start + timedelta(minutes=ws)
        win_end = day_start + timedelta(minutes=we)
        ov_start = max(cur, win_start)
        ov_end = min(end, win_end)
        if ov_end > ov_start:
            total += (ov_end - ov_start).total_seconds() / 60.0
        cur = day_start + timedelta(days=1)
    return total


def _color_for_response(minutes: float | None) -> str:
    if minutes is None:
        return "#6b7280"
    if minutes <= 5:
        return "#00b42a"
    if minutes <= 20:
        return "#ff7d00"
    return "#f53f3f"


# ------------------------------------------------------------------
# AI \u5468\u62a5\uff1a\u6307\u5b9a\u65f6\u95f4\u8303\u56f4 + \u53ef\u9009\u90e8\u95e8 + \u5468\u6bd4\uff08\u4ec5\u8d85\u7ba1\uff09
# ------------------------------------------------------------------
def _range_key(start_dt: datetime) -> str:
    return start_dt.strftime("%Y-%m-%d")


def _range_label(start_dt: datetime, end_dt: datetime) -> str:
    return f"{start_dt.strftime('%m月%d日')} \u2013 {end_dt.strftime('%m月%d日')}"


async def _compute_range_stats(start_dt: datetime, end_dt: datetime, dept: str) -> dict[str, Any]:
    whs = community_store.get_workhours()
    now = datetime.now()
    created_this_week = 0
    processed_count = 0
    completed_count = 0
    unprocessed = 0
    type_dist: dict[str, int] = {}
    dup_map: dict[tuple[str, str], list[str]] = {}
    overdue: list[dict[str, Any]] = []
    backlog: list[dict[str, Any]] = []
    dept_metrics_raw: dict[str, dict[str, Any]] = {}

    resp_vals: dict[str, list[float]] = {}
    hand_vals: dict[str, list[float]] = {}
    all_resp: list[float] = []
    all_hand: list[float] = []

    async with _task_lock:
        _refresh_tasks()
        for task in _tasks.values():
            created = task.get("created_at", "")
            cdt = _parse_dt(created)
            if cdt is None:
                continue
            created_in_range = start_dt <= cdt <= end_dt
            if not created_in_range:
                continue
            # 部门筛选：仅统计派给该部门的工单
            if dept and task.get("assigned_dept", "") != dept:
                continue
            # \u5e9f\u5f03\u5de5\u5355\u4e0d\u53c2\u4e0e\u6307\u6807
            if task.get("status") in ("\u5df2\u62d2\u7edd", "\u5df2\u64a4\u9500"):
                continue

            created_this_week += 1
            et = task.get("event_type", "") or "-"
            type_dist[et] = type_dist.get(et, 0) + 1

            # \u673a\u4f1a\u98ce\u9669\u7c7b\u522b
            if task.get("status") in ("\u5f85\u5ba1\u6838", "\u5f85\u5904\u7406", "\u5904\u7406\u8d85\u65f6"):
                unprocessed += 1
            if task.get("status") == "\u5904\u7406\u8d85\u65f6":
                overdue.append({"event_id": task.get("event_id", ""), "type": et, "created_at": created})
            # \u79ef\u538b\uff1a\u521b\u5efa\u8d85 3 \u5929\u4ecd\u672a\u5b8c\u6210
            if task.get("status") != "\u5df2\u5b8c\u6210" and cdt < now - timedelta(days=3):
                backlog.append({"event_id": task.get("event_id", ""), "type": et, "created_at": created})

            key = (task.get("user_id", ""), et)
            dup_map.setdefault(key, []).append(task.get("event_id", ""))

            # 本周是否进入处理（已受理/处理中/已完成 时间线在本周）
            processed_in_week = False
            for n in (task.get("timeline") or []):
                t = _parse_dt(n.get("time", "")) if n.get("time") else None
                if t and start_dt <= t <= end_dt and n.get("type") in ("已受理", "处理中", "已完成"):
                    processed_in_week = True
                    break
            if processed_in_week:
                processed_count += 1

            # \u4e3b\u4f53\u6307\u6807\u4ec5\u7528\u5df2\u5b8c\u6210\uff08\u529e\u7ed3\uff09\u5de5\u5355
            if task.get("status") != "\u5df2\u5b8c\u6210" or not task.get("completed_at"):
                continue
            cplt = _parse_dt(task["completed_at"])
            if cplt is None or not (start_dt <= cplt <= end_dt):
                continue
            completed_count += 1

            ad = task.get("assigned_dept", "")
            rv_dept = task.get("reviewer_dept", "")
            if not ad or ad not in dispatch_agent.DEPARTMENTS:
                continue
            if rv_dept not in dispatch_agent.DEPARTMENTS:
                continue  # \u8d85\u7ba1\u5b8c\u6210\u4e0d\u7eb3\u5165

            first_handle = None
            for n in (task.get("timeline") or []):
                if n.get("type") == "\u5f85\u5904\u7406" and n.get("time"):
                    t = _parse_dt(n["time"])
                    if t is not None:
                        first_handle = t
                        break
            if first_handle is None:
                continue
            resp_min = _work_minutes_between(cdt, first_handle, whs)
            hand_min = _work_minutes_between(first_handle, cplt, whs)
            if resp_min is None or hand_min is None or resp_min < 0 or hand_min < 0:
                continue  # \u65f6\u95f4\u810f\u6570\u636e\u5254\u9664

            if dept and ad != dept:
                continue
            di = dept_metrics_raw.setdefault(ad, {"count": 0, "resp": [], "hand": []})
            di["count"] += 1
            di["resp"].append(resp_min)
            di["hand"].append(hand_min)
            all_resp.append(resp_min)
            all_hand.append(hand_min)

    # \u6c47\u603b
    def _avg(vals):
        return round(sum(vals) / len(vals), 1) if vals else None

    dept_metrics = []
    for k in dispatch_agent.DEPARTMENTS:
        di = dept_metrics_raw.get(k)
        dept_metrics.append({
            "dept_key": k,
            "dept_name": dispatch_agent.DEPARTMENTS[k],
            "count": di["count"] if di else 0,
            "avg_response_min": _avg(di["resp"]) if di else None,
            "avg_handling_min": _avg(di["hand"]) if di else None,
        })

    duplicates = []
    for (uid, et), ids in dup_map.items():
        if len(ids) >= 2:
            duplicates.append({"user_id": uid, "type": et, "count": len(ids), "event_ids": ids})
    duplicates.sort(key=lambda x: x["count"], reverse=True)

    avg_response = _avg(all_resp)
    avg_handling = _avg(all_hand)
    completion_rate = round(completed_count / created_this_week, 4) if created_this_week else None
    sla_compliance = round(sum(1 for r in all_resp if r <= 20) / len(all_resp), 4) if all_resp else None
    hot_types = sorted(type_dist.items(), key=lambda x: x[1], reverse=True)[:3]

    return {
        "total_created": created_this_week,
        "processed_count": processed_count,
        "completed_count": completed_count,
        "unprocessed_count": unprocessed,
        "overdue_count": len(overdue),
        "backlog_count": len(backlog),
        "completion_rate": completion_rate,
        "avg_response_min": avg_response,
        "avg_handling_min": avg_handling,
        "response_color": _color_for_response(avg_response),
        "handling_color": _color_for_response(avg_handling),
        "sla_compliance_rate": sla_compliance,
        "work_hours_start": whs.get("work_hours_start", "09:00"),
        "work_hours_end": whs.get("work_hours_end", "18:00"),
        "type_distribution": type_dist,
        "hot_types": [{"type": t, "count": c} for t, c in hot_types],
        "dept_metrics": dept_metrics,
        "duplicates": duplicates,
        "overdue": overdue,
        "backlog": backlog,
    }


async def _build_weekly_report(start_date: str = "", end_date: str = "", dept: str = "") -> dict[str, Any]:
    now = datetime.now()
    if start_date:
        start_dt = _parse_dt(start_date + " 00:00:00")
        end_dt = _parse_dt((end_date or start_date) + " 23:59:59")
    else:
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = monday
        end_dt = now
    if start_dt is None or end_dt is None or end_dt < start_dt:
        end_dt = start_dt + timedelta(days=6)

    stats = await _compute_range_stats(start_dt, end_dt, dept)
    prev_end = start_dt - timedelta(seconds=1)
    prev_start = start_dt - timedelta(days=7)
    prev_stats = await _compute_range_stats(prev_start, prev_end, dept)

    week_key = _range_key(start_dt)
    week_label = _range_label(start_dt, end_dt)
    ai_summary = ""

    ai = await asyncio.to_thread(
        weekly_report.generate_ai_summary,
        stats,
        prev_stats,
        label=week_label,
    )
    ai_summary = ai.get("ai_summary", "")

    report = {
        "week_key": week_key,
        "week_label": week_label,
        "start": start_dt.strftime("%Y-%m-%d"),
        "end": end_dt.strftime("%Y-%m-%d"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "dept": dept,
        "stats": stats,
        "prev": {
            "avg_response_min": prev_stats.get("avg_response_min"),
            "avg_handling_min": prev_stats.get("avg_handling_min"),
            "completed_count": prev_stats.get("completed_count"),
            "completion_rate": prev_stats.get("completion_rate"),
        },
        "ai_summary": ai_summary,
    }
    weekly_report.save(week_key, report)
    return report


@app.get("/api/admin/weekly_reports")
async def admin_weekly_reports(
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    return {"success": True, "weeks": weekly_report.load_weeks()}


@app.get("/api/admin/weekly_report")
async def admin_weekly_report_get(
    week: str = "",
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    wk = week or datetime.now().strftime("%Y-%m-%d")
    data = weekly_report.load(wk)
    if data is None:
        raise HTTPException(status_code=404, detail="该周周报尚未生成")
    return {"success": True, "report": data}


@app.post("/api/admin/weekly_report")
async def admin_weekly_report_post(
    start: str = "",
    end: str = "",
    dept: str = "",
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    report = await _build_weekly_report(start, end, dept)
    return {"success": True, "report": report}


@app.get("/api/admin/weekly_report/export")
async def admin_weekly_report_export(
    week: str = "",
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> Response:
    wk = week or datetime.now().strftime("%Y-%m-%d")
    data = weekly_report.load(wk)
    if data is None:
        raise HTTPException(status_code=404, detail="该周周报尚未生成")
    md = weekly_report.to_markdown(data)
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="weekly_report_{wk}.md"'},
    )


# ------------------------------------------------------------------
# ------------------------------------------------------------------
# \u7cfb\u7edf\u516c\u544a\uff08\u4ec5\u8d85\u7ba1\u7ba1\u7406\uff1b\u5c45\u6c11\u8bfb\u516c\u544a\uff09
# ------------------------------------------------------------------
class AnnouncementRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="\u516c\u544a\u6807\u9898")
    content: str = Field(..., max_length=20000, description="\u516c\u544a\u6b63\u6587\uff08\u5bcc\u6587\u672c\uff09")
    publish_time: str = Field(..., description="\u751f\u6548\u65f6\u95f4 YYYY-MM-DD HH:MM[:SS]")
    expire_time: str = Field(..., description="\u5230\u671f\u65f6\u95f4 YYYY-MM-DD HH:MM[:SS]")


def _norm_ann_dt(s: str) -> str:
    s = (s or "").replace("T", " ").strip()
    if len(s) == 16:
        s = s + ":00"
    return s


@app.get("/api/admin/announcements")
async def admin_list_announcements(
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    return {"success": True, "announcements": db.list_announcements()}


@app.post("/api/admin/announcements")
async def admin_create_announcement(
    body: AnnouncementRequest,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    a = db.create_announcement(
        body.title, body.content,
        _norm_ann_dt(body.publish_time), _norm_ann_dt(body.expire_time),
        _admin.get("id", ""),
    )
    return {"success": True, "announcement": a}


@app.put("/api/admin/announcements/{announcement_id}")
async def admin_update_announcement(
    announcement_id: int,
    body: AnnouncementRequest,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    a = db.update_announcement(
        announcement_id, body.title, body.content,
        _norm_ann_dt(body.publish_time), _norm_ann_dt(body.expire_time),
    )
    if a is None:
        raise HTTPException(status_code=404, detail="\u516c\u544a\u4e0d\u5b58\u5728")
    return {"success": True, "announcement": a}


@app.delete("/api/admin/announcements/{announcement_id}")
async def admin_delete_announcement(
    announcement_id: int,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    if not db.delete_announcement(announcement_id):
        raise HTTPException(status_code=404, detail="\u516c\u544a\u4e0d\u5b58\u5728")
    return {"success": True}


@app.get("/api/announcements/unread")
async def user_unread_announcements(
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    return {"success": True, "announcements": db.list_unread_announcements(current_user.get("id", ""))}


@app.post("/api/announcements/{announcement_id}/read")
async def user_read_announcement(
    announcement_id: int,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    db.mark_announcement_read(current_user.get("id", ""), announcement_id)
    return {"success": True}


# ------------------------------------------------------------------

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
        role = current_user.get("role")
        dept = current_user.get("department", "")
        for task in _tasks.values():
            if role == "resident" and task.get("user_id") != current_user.get("id"):
                continue
            if role == "dept":
                if task.get("assigned_dept", "") != dept and task.get("reviewer_dept", "") != dept and task.get("reviewer_id", "") != current_user.get("id"):
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
            if role == "resident":
                staff_replies = [r for r in replies if r.get("role") in ("admin", "dept")]
                if staff_replies:
                    last_reply_at = staff_replies[-1].get("created_at", "")
                    user_read_at = task.get("user_read_at", "")
                    if not user_read_at or last_reply_at > user_read_at:
                        has_new_reply = True
            elif role in ("admin", "dept"):
                resident_replies = [r for r in replies if r.get("role") == "resident"]
                if resident_replies:
                    last_reply_at = resident_replies[-1].get("created_at", "")
                    dept_read_at = task.get("dept_read_at", "")
                    if not dept_read_at or last_reply_at > dept_read_at:
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
                "assigned_dept": task.get("assigned_dept", ""),
                "department_name": task.get("department_name", ""),
                "audio_transcript": task.get("audio_transcript", ""),
                "timeline": task.get("timeline", []),
                "media": task.get("media", []),
                "reviewer_id": task.get("reviewer_id", ""),
                "reviewer_dept": task.get("reviewer_dept", ""),
                "returned_by_dept": task.get("returned_by_dept", ""),
                "returned_by_dept_name": task.get("returned_by_dept_name", ""),
                "dispatched_by_name": task.get("dispatched_by_name", ""),
            }
            # 定位坐标/距中心米数仅管理员/部门可见，居民端不返回（避免暴露他人位置）
            if role in ("admin", "dept"):
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
        building = (body.building or "").strip()
        unit = (body.unit or "").strip()
        room = (body.room or "").strip()
        media_entries = [{
            "media_id": m,
            "kind": "audio" if media_store.is_audio(m) else "photo",
            "uploaded_by": current_user.get("id", ""),
            "created_at": "",
            "note": "",
        } for m in (body.media_ids or []) if m]
        # 描述或录音至少其一；带录音的事件一律不拒绝，先进人工审核
        has_audio = any(media_store.is_audio(m) for m in (body.media_ids or []))
        if not (body.description or "").strip() and not has_audio:
            return EventResponse(success=False, error="请填写事件描述或提供录音")
        if has_audio:
            event_id = str(uuid.uuid4())
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            async with _task_lock:
                _refresh_tasks()
                _tasks[event_id] = _build_task(
                    event_id=event_id, description=(body.description or "").strip(), created_at=created_at,
                    status="待审核", address="", event_type="待审核", urgency="中", scene_tag="常规",
                    emergency_type="", user=current_user, lat=body.lat, lng=body.lng, beneficiary=beneficiary,
                )
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
                _save_tasks(_tasks)
            audio_state = {
                "description": (body.description or "").strip(), "address": "", "event_type": "待审核",
                "urgency": "中", "scene_tag": "常规", "handler": "", "status": "待审核",
                "created_at": "", "user_id": current_user["id"], "confidence": "none",
                "confirmation_required": False, "emergency_type": "", "confirmed": False,
            }
            # 录音事件：同步完成 ASR 转写 + 识别派单，确保提交结果与后台一致
            await _process_event(event_id, audio_state, current_user["id"], body.lat, body.lng)
            async with _task_lock:
                _refresh_tasks()
                _task = _tasks.get(event_id)
            _final = _task or {}
            return EventResponse(
                success=True,
                data=EventResponseData(
                    event_id=event_id,
                    address=_final.get("address", "") or "",
                    event_type=_final.get("event_type", "待审核") or "待审核",
                    urgency=_final.get("urgency", "中") or "中",
                    scene_tag=_final.get("scene_tag", "常规") or "常规",
                    handler=_final.get("handler", "") or "",
                    status=_final.get("status", "待审核") or "待审核",
                    created_at=created_at,
                ),
            )
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
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                if _tasks[event_id].get("timeline"):
                    _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
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
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                if _tasks[event_id].get("timeline"):
                    _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
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
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                if _tasks[event_id].get("timeline"):
                    _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
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
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                if _tasks[event_id].get("timeline"):
                    _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
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
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                if _tasks[event_id].get("timeline"):
                    _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
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
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                if _tasks[event_id].get("timeline"):
                    _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
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
                status="待处理",
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
            _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
            if _tasks[event_id].get("timeline"):
                _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
            _tasks[event_id]["event_building"] = building
            _tasks[event_id]["event_unit"] = unit
            _tasks[event_id]["event_room"] = room
            _save_tasks(_tasks)

        # 启动后台异步任务，传入已校验结果，避免二次调用 LLM API
        semantic_result["confirmed"] = body.confirmed
        semantic_result["status"] = "待处理"
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
                status="待处理",
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
                _tasks[event_id]["media"] = [{**m, "created_at": created_at} for m in media_entries]
                if _tasks[event_id].get("timeline"):
                    _tasks[event_id]["timeline"][0]["photos"] = [m.get("media_id", "") for m in media_entries]
                _tasks[event_id]["event_building"] = building
                _tasks[event_id]["event_unit"] = unit
                _tasks[event_id]["event_room"] = room
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
@app.get("/api/events/{event_id}")
async def get_event(
    event_id: str,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    """
    按事件标识查询处理状态、时间线、媒体与回复。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)

    if task is None:
        raise HTTPException(status_code=404, detail="事件不存在")

    role = current_user.get("role")
    dept = current_user.get("department", "")
    if role == "resident" and task.get("user_id") != current_user.get("id"):
        raise HTTPException(status_code=403, detail="无权访问该事件")
    if role == "dept" and task.get("assigned_dept", "") != dept and task.get("reviewer_dept", "") != dept and task.get("reviewer_id", "") != current_user.get("id"):
        raise HTTPException(status_code=403, detail="无权访问该事件")

    return {
        "event_id": task["event_id"],
        "description": task["description"],
        "status": task["status"],
        "address": task.get("address") or None,
        "event_type": task.get("event_type") or None,
        "urgency": task.get("urgency") or None,
        "scene_tag": task.get("scene_tag") or None,
        "emergency_type": task.get("emergency_type") or None,
        "handler": task.get("handler") or None,
        "department_name": task.get("department_name", ""),
        "assigned_dept": task.get("assigned_dept", ""),
        "audio_transcript": task.get("audio_transcript", ""),
        "created_at": task["created_at"],
        "completed_at": task.get("completed_at"),
        "error": task.get("error"),
        "reply": task.get("reply") or None,
        "replies": task.get("replies", []),
        "timeline": task.get("timeline", []),
        "media": task.get("media", []),
        "rejected_reason": task.get("rejected_reason", ""),
        "withdrawn_at": task.get("withdrawn_at", ""),
        "returned_by_dept": task.get("returned_by_dept", ""),
        "returned_by_dept_name": task.get("returned_by_dept_name", ""),
        "dispatched_by_name": task.get("dispatched_by_name", ""),
    }


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
    current_user: dict[str, Any] = Depends(get_staff_dependency),
) -> dict[str, Any]:
    """
    工作人员受理待处理/待审核事件，状态更新为"已受理"。
    部门账号只能受理本部门事件；超管可受理外部资源与待审核事件。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "dept" and task.get("assigned_dept", "") != dept:
            raise HTTPException(status_code=403, detail="无权操作该事件")
        if task.get("status") == "待审核":
            raise HTTPException(status_code=400, detail="待审核事件请先归类（修改事件类型）后再受理")
        if task.get("status") != "待处理":
            raise HTTPException(status_code=400, detail="仅待处理事件可受理")
        task["status"] = "已受理"
        task["reviewer_id"] = current_user.get("id", "")
        task["reviewer_dept"] = dept
        if request and request.reply:
            _append_reply(task, request.reply, current_user)
        _timeline_append(task, "已受理", "已受理", current_user.get("real_name", ""))
        _save_tasks(_tasks)

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
    current_user: dict[str, Any] = Depends(get_staff_dependency),
) -> dict[str, Any]:
    """
    工作人员拒绝待处理/待审核事件，状态更新为"已拒绝"并记录理由。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "dept" and task.get("assigned_dept", "") != dept:
            raise HTTPException(status_code=403, detail="无权操作该事件")
        if task.get("status") not in ("待处理", "待审核"):
            raise HTTPException(status_code=400, detail="仅待处理或待审核事件可拒绝")
        task["status"] = "已拒绝"
        task["rejected_reason"] = request.reason
        task["reply"] = request.reason
        task["rejected_at"] = _now()
        task["rejected_by"] = current_user.get("id", "")
        _timeline_append(task, "已拒绝", "事件被拒绝：" + request.reason, current_user.get("real_name", ""))
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
# API 端点：POST /api/events/{event_id}/return
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/return")
async def return_event_to_admin(
    event_id: str,
    current_user: dict[str, Any] = Depends(get_staff_dependency),
) -> dict[str, Any]:
    """部门把事件转回超管，重置为「待审核」让超管重新归类派单。"""
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "dept" and task.get("assigned_dept", "") != dept and task.get("reviewer_dept", "") != dept and task.get("reviewer_id", "") != current_user.get("id"):
            raise HTTPException(status_code=403, detail="无权操作该事件")
        if task.get("status") != "待处理":
            raise HTTPException(status_code=400, detail="仅待处理事件可转回管理")
        task["status"] = "待审核"
        task["event_type"] = "待审核"
        task["assigned_dept"] = ""
        task["department_name"] = ""
        task["handler"] = ""
        task["reviewer_id"] = ""
        task["reviewer_dept"] = ""
        task["returned_by_dept"] = dept if role == "dept" else ""
        task["returned_by_dept_name"] = dispatch_agent.DEPARTMENTS.get(dept, "") if role == "dept" else ""
        task["dispatched_by_name"] = ""
        _timeline_append(task, "转回管理", "事件已转回管理，请重新归类派单", current_user.get("real_name", ""))
        _save_tasks(_tasks)
    return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"]}}


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
    多轮对话：工作人员回复（文字+可选照片）或居民追问，不改变事件状态。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")

        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "resident":
            if task.get("user_id") != current_user.get("id"):
                raise HTTPException(status_code=403, detail="无权操作该事件")
            if task.get("status") not in ("已受理", "处理中", "已完成"):
                raise HTTPException(status_code=400, detail="仅已受理/处理中/已完成事件可追问")
            _append_reply(task, request.reply, current_user, request.photos)
            _timeline_append(task, "追问", "居民追问", current_user.get("real_name", ""), request.photos)
        elif role in ("admin", "dept"):
            if role == "dept" and task.get("assigned_dept", "") != dept and task.get("reviewer_dept", "") != dept:
                raise HTTPException(status_code=403, detail="无权操作该事件")
            _append_reply(task, request.reply, current_user, request.photos)
            _timeline_append(task, "回复", "工作人员回复", current_user.get("real_name", ""), request.photos)
        else:
            raise HTTPException(status_code=403, detail="无权操作该事件")
        _save_tasks(_tasks)

    return {
        "success": True,
        "data": {
            "event_id": task["event_id"],
            "reply": task["reply"],
        },
    }


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/start（开始处理，可选）
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/start")
async def start_event(
    event_id: str,
    current_user: dict[str, Any] = Depends(get_staff_dependency),
) -> dict[str, Any]:
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "dept" and task.get("assigned_dept", "") != dept:
            raise HTTPException(status_code=403, detail="无权操作该事件")
        if task.get("status") != "已受理":
            raise HTTPException(status_code=400, detail="仅已受理事件可开始处理")
        task["status"] = "处理中"
        _timeline_append(task, "处理中", "开始处理", current_user.get("real_name", ""))
        _save_tasks(_tasks)
    return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"]}}


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/complete（完成，强制照片留证）
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/complete")
async def complete_event(
    event_id: str,
    request: CompleteRequest,
    current_user: dict[str, Any] = Depends(get_staff_dependency),
) -> dict[str, Any]:
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "dept" and task.get("assigned_dept", "") != dept and task.get("reviewer_dept", "") != dept:
            raise HTTPException(status_code=403, detail="无权操作该事件")
        if task.get("status") not in ("已受理", "处理中"):
            raise HTTPException(status_code=400, detail="仅已受理或处理中事件可完成")
        if not request.photos:
            raise HTTPException(status_code=400, detail="完成事件必须上传至少 1 张留证照片")
        task["status"] = "已完成"
        task["reviewer_id"] = current_user.get("id", "")
        task["reviewer_dept"] = dept
        task["completed_at"] = _now()
        if request.reply:
            _append_reply(task, request.reply, current_user, request.photos)
        _timeline_append(task, "已完成", "处理完成" + (("：" + request.reply) if request.reply else ""), current_user.get("real_name", ""), request.photos)
        _save_tasks(_tasks)

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
        logger.warning("完成记录写入失败：event_id=%s，异常=%s", event_id, exc)

    return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"]}}


# ------------------------------------------------------------------
# API 端点：PATCH /api/events/{event_id}/type（手动修正类型并自动重派）
# ------------------------------------------------------------------
@app.patch("/api/events/{event_id}/type")
async def update_event_type(
    event_id: str,
    request: TypeUpdateRequest,
    current_user: dict[str, Any] = Depends(get_staff_dependency),
) -> dict[str, Any]:
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "dept" and task.get("assigned_dept", "") != dept:
            raise HTTPException(status_code=403, detail="无权操作该事件")
        old_type = task.get("event_type", "")
        new_type = request.event_type.strip()
        TERMINAL = ("已完成", "已拒绝", "已撤销")
        if task.get("status") in TERMINAL:
            raise HTTPException(status_code=400, detail="已完成/已拒绝/已撤销的事件不可修改类型")
        valid_types = set(dispatch_agent.EVENT_TYPE_TO_HANDLER.keys()) | {"待审核"}
        if new_type not in valid_types:
            raise HTTPException(status_code=400, detail="事件类型不合法")
        # 类型与紧急度均未变化：不做任何派单/时间线，直接返回
        changed = new_type != old_type or bool(request.urgency and request.urgency != task.get("urgency", ""))
        if not changed:
            return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"], "event_type": new_type, "department_name": task.get("department_name", "")}}
        if request.urgency and request.urgency in ("高", "中", "低"):
            task["urgency"] = request.urgency
        task["event_type"] = new_type
        if new_type == "待审核":
            task["handler"] = "人工部"
            task["assigned_dept"] = ""
            task["department_name"] = ""
            task["status"] = "待审核"
            _timeline_append(task, "改类型", f"事件类型由 {old_type} 调整为待审核", current_user.get("real_name", ""))
        else:
            handler, key, name = dispatch_agent.event_type_to_department(new_type, task.get("urgency", ""), task.get("scene_tag", ""), task.get("emergency_type", ""))
            task["handler"] = handler
            task["assigned_dept"] = key
            task["department_name"] = name
            task["status"] = "待处理"
            task["returned_by_dept"] = ""
            task["returned_by_dept_name"] = ""
            task["dispatched_by_name"] = current_user.get("real_name", "")
            _timeline_append(task, "改类型", f"事件类型由 {old_type} 调整为 {new_type}，已转派 {name}", current_user.get("real_name", ""))
        _save_tasks(_tasks)
    return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"], "event_type": task["event_type"], "department_name": task.get("department_name", "")}}


# ------------------------------------------------------------------
# API 端点：PATCH /api/events/{event_id}/dept（超管改派部门）
# ------------------------------------------------------------------
@app.patch("/api/events/{event_id}/dept")
async def update_event_dept(
    event_id: str,
    request: DeptUpdateRequest,
    current_user: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        dept = request.department.strip()
        if dept not in dispatch_agent.DEPARTMENTS:
            raise HTTPException(status_code=400, detail="部门不合法")
        TERMINAL = ("已完成", "已拒绝", "已撤销")
        if task.get("status") in TERMINAL:
            raise HTTPException(status_code=400, detail="已完成/已拒绝/已撤销的事件不可改派")
        if task.get("event_type", "") == "待审核":
            raise HTTPException(status_code=400, detail="待审核事件请先修改类型后再改派/受理")
        if dept == task.get("assigned_dept", ""):
            return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"], "department_name": task.get("department_name", "")}}
        name = dispatch_agent.DEPARTMENTS[dept]
        task["assigned_dept"] = dept
        task["department_name"] = name
        task["handler"] = name
        task["dispatched_by_name"] = current_user.get("real_name", "")
        # 改派后由新部门重新受理，清空原受理进度
        task["status"] = "待处理"
        task["reviewer_id"] = ""
        task["reviewer_dept"] = ""
        _timeline_append(task, "改派", f"事件已改派至 {name}", current_user.get("real_name", ""))
        _save_tasks(_tasks)
    return {"success": True, "data": {"event_id": task["event_id"], "status": task["status"], "department_name": name}}


@app.delete("/api/admin/dept_users/{user_id}")
async def admin_delete_dept_user(
    user_id: str,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    ok, msg = auth.delete_dept_user(user_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg}


# ------------------------------------------------------------------
# API 端点：媒体上传 / 读取（COS 或本地回退）
# ------------------------------------------------------------------
@app.post("/api/uploads")
async def upload_media(
    file: UploadFile = File(...),
    kind: str = Form("photo"),
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    data = await file.read()
    try:
        media_id = media_store.save_upload(data, file.filename, kind=kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"success": True, "data": {"media_id": media_id, "kind": kind, "url": f"/api/media/{media_id}"}}


@app.get("/api/media/{media_id}")
async def get_media(
    media_id: str,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> Response:
    data = media_store.read_upload(media_id)
    if data is None:
        raise HTTPException(status_code=404, detail="媒体不存在")
    return Response(content=data, media_type=media_store.content_type(media_id))


# ------------------------------------------------------------------
# API 端点：部门账号管理（仅超管）
# ------------------------------------------------------------------
@app.get("/api/admin/dept_users")
async def admin_list_dept_users(
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> list[dict[str, Any]]:
    return auth.list_dept_users()


@app.post("/api/admin/dept_users")
async def admin_create_dept_user(
    body: DeptUserCreateRequest,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    ok, msg, user = auth.create_dept_user(
        username=body.username, password=body.password, real_name=body.real_name,
        phone=body.phone, department=body.department,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "data": user}


@app.patch("/api/admin/dept_users/{user_id}")
async def admin_update_dept_user(
    user_id: str,
    body: DeptUserUpdateRequest,
    _admin: dict[str, Any] = Depends(get_admin_dependency),
) -> dict[str, Any]:
    ok, msg, user = auth.update_dept_user(
        user_id,
        username=body.username,
        real_name=body.real_name,
        phone=body.phone,
        password=body.password,
        department=body.department,
        status=body.status,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "data": user}


# ------------------------------------------------------------------
# API 端点：未读提示
# ------------------------------------------------------------------
@app.get("/api/notifications/unread")
async def unread_notifications(
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    async with _task_lock:
        _refresh_tasks()
        role = current_user.get("role")
        dept = current_user.get("department", "")
        pending = 0
        new_reply = 0
        for task in _tasks.values():
            replies = task.get("replies") or []
            if role == "resident":
                if task.get("user_id") != current_user.get("id"):
                    continue
                staff = [r for r in replies if r.get("role") in ("admin", "dept")]
                if staff and staff[-1].get("created_at", "") > (task.get("user_read_at") or ""):
                    new_reply += 1
            elif role == "dept":
                if task.get("assigned_dept", "") != dept and task.get("reviewer_dept", "") != dept and task.get("reviewer_id", "") != current_user.get("id"):
                    continue
                if task.get("status") == "待处理" and task.get("assigned_dept", "") == dept:
                    pending += 1
                resident_msgs = [r for r in replies if r.get("role") == "resident"]
                if resident_msgs and resident_msgs[-1].get("created_at", "") > (task.get("dept_read_at") or ""):
                    new_reply += 1
            else:
                if task.get("status") == "待审核":
                    pending += 1
                if task.get("status") == "待处理" and not task.get("assigned_dept"):
                    pending += 1
                resident_msgs = [r for r in replies if r.get("role") == "resident"]
                if resident_msgs and resident_msgs[-1].get("created_at", "") > (task.get("dept_read_at") or ""):
                    new_reply += 1
        return {"success": True, "pending": pending, "new_reply": new_reply}


# ------------------------------------------------------------------
# API 端点：POST /api/events/{event_id}/mark_read
# ------------------------------------------------------------------
@app.post("/api/events/{event_id}/mark_read")
async def mark_event_read(
    event_id: str,
    current_user: dict[str, Any] = Depends(get_current_user_dependency),
) -> dict[str, Any]:
    """
    居民查看回复后标记 user_read_at；工作人员查看追问后标记 dept_read_at。
    """
    async with _task_lock:
        _refresh_tasks()
        task = _tasks.get(event_id)
        if task is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        role = current_user.get("role")
        dept = current_user.get("department", "")
        if role == "resident":
            if task.get("user_id") != current_user.get("id"):
                raise HTTPException(status_code=403, detail="无权访问此事件")
            task["user_read_at"] = _now()
        elif role in ("admin", "dept"):
            if role == "dept" and task.get("assigned_dept", "") != dept and task.get("reviewer_dept", "") != dept and task.get("reviewer_id", "") != current_user.get("id"):
                raise HTTPException(status_code=403, detail="无权访问此事件")
            task["dept_read_at"] = _now()
        else:
            raise HTTPException(status_code=403, detail="无权访问此事件")
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
