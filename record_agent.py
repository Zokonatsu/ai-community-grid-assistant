"""
record_agent.py
记录Agent模块（LangGraph单Agent）

功能：将事件处理的完整结果（含原始描述、提取信息、派单结果等）持久化保存到本地 JSONL 文件，
      便于后续审计、统计分析和历史回溯。

模块接口约定：
  输入: 上游Agent的完整输出（含 description, address, event_type, urgency, handler）
  输出: 追加了 status="已派单" 和 created_at 时间戳的完整记录

持久化格式：
  - 文件路径：./data/events.jsonl
  - 每行一条 JSON 记录（JSON Lines 格式），追加写入不覆盖历史
  - 目录不存在时自动创建
"""

import json
import logging
import os
import threading
from datetime import datetime
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

# ------------------------------------------------------------------
# 配置日志记录器
# ------------------------------------------------------------------
logger = logging.getLogger("record_agent")

# ------------------------------------------------------------------
# 并发写入锁
# ------------------------------------------------------------------
# 多线程（如 FastAPI 并发请求）同时追加写入同一文件时，
# 必须使用互斥锁保证数据完整性，防止记录交错或丢失。
_write_lock = threading.Lock()


# ------------------------------------------------------------------
# State 定义
# ------------------------------------------------------------------
class RecordState(TypedDict):
    """
    记录Agent的状态类型定义。

    字段说明：
        description: 居民提交的原始问题描述。
        address:     从描述中提取的具体地址。
        event_type:  事件分类（物业维修/环境卫生/安全隐患/邻里纠纷/公共设施/其他/待审核）。
        urgency:     紧急程度（高/中/低）。
        scene_tag:   场景标签（生命急救/紧急救援/常规），影响派单行为。
        handler:     派发Agent分配的处理部门（如"物业部"、"[紧急]安保部"等）。
        status:      记录状态。若上游已传入（如"待审核"），则保留原值；否则默认为"已派单"。
        created_at:  记录创建时间，由本模块填充为当前时间，格式"YYYY-MM-DD HH:MM:SS"。
        user_id:     提交事件的用户标识，用于数据隔离与审计追溯。
        confidence:  语义校验置信度（high/medium/low/none），由接收Agent传入。
        reply:       后台人工回复内容，供用户前端查看。
    """
    description: str
    address: str
    event_type: str
    urgency: str
    scene_tag: str
    handler: str
    status: str
    created_at: str
    user_id: str
    confidence: str
    reply: str


# ------------------------------------------------------------------
# 持久化配置
# ------------------------------------------------------------------
# 数据文件存放目录，相对于当前工作目录
DATA_DIR = "./data"

# JSONL 文件完整路径，每条记录占一行，便于追加和流式读取
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")


def _ensure_data_dir() -> None:
    """
    确保数据目录存在，不存在时自动创建。

    使用 os.makedirs 的 exist_ok=True 参数，避免并发或重复创建时抛出异常。
    """
    os.makedirs(DATA_DIR, exist_ok=True)


# ------------------------------------------------------------------
# 节点函数：record_node
# ------------------------------------------------------------------
def record_node(state: RecordState) -> RecordState:
    """
    LangGraph 节点：将事件记录持久化到本地 JSONL 文件。

    执行流程：
        1. 从 state 中读取上游字段（description, address, event_type, urgency, handler）。
        2. 补充 status 字段，固定为"已派单"。
        3. 补充 created_at 字段，使用当前系统时间，格式化为"YYYY-MM-DD HH:MM:SS"。
        4. 将完整记录序列化为 JSON 字符串，追加写入 events.jsonl 文件。
        5. 返回包含所有字段的完整 State。

    参数:
        state: 当前图状态，至少包含 description, address, event_type, urgency, handler 字段。

    返回:
        更新后的 RecordState，其中 status 和 created_at 已被填充。
    """
    # 读取上游传递的核心字段，若缺失则使用空字符串兜底
    description = state.get("description", "")
    address = state.get("address", "")
    event_type = state.get("event_type", "")
    urgency = state.get("urgency", "")
    scene_tag = state.get("scene_tag", "常规")
    handler = state.get("handler", "")
    user_id = state.get("user_id", "")
    confidence = state.get("confidence", "")
    reply = state.get("reply", "")

    # 补充状态字段：若上游已传入有效状态（如"待审核"），保留原值；否则默认为"已派单"
    upstream_status = state.get("status", "")
    status = upstream_status if upstream_status else "已派单"

    # 补充创建时间字段：当前系统时间，格式"YYYY-MM-DD HH:MM:SS"
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 构建完整记录字典
    record = {
        "description": description,
        "address": address,
        "event_type": event_type,
        "urgency": urgency,
        "scene_tag": scene_tag,
        "handler": handler,
        "status": status,
        "created_at": created_at,
        "user_id": user_id,
        "confidence": confidence,
        "reply": reply,
    }

    # 持久化到 JSONL 文件：锁保护 + 异常降级
    # 无论文件写入成功或失败，均返回完整状态，确保上游链路不中断
    try:
        _ensure_data_dir()
        with _write_lock:
            with open(EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except (OSError, IOError, TypeError, ValueError) as exc:
        # OSError/IOError: 磁盘满、权限不足、路径不可写等
        # TypeError/ValueError: json.dumps 序列化异常（理论上不会发生，防御性捕获）
        logger.error(
            "事件持久化失败，已优雅降级。文件='%s'，异常类型=%s，异常信息=%s",
            EVENTS_FILE,
            type(exc).__name__,
            exc,
        )
    except Exception as exc:
        # 兜底：捕获所有未预期异常，确保程序绝不崩溃
        logger.error(
            "事件持久化发生未预期异常，已优雅降级。异常类型=%s，异常信息=%s",
            type(exc).__name__,
            exc,
        )

    # 返回包含所有字段的完整状态
    return {
        "description": description,
        "address": address,
        "event_type": event_type,
        "urgency": urgency,
        "scene_tag": scene_tag,
        "handler": handler,
        "status": status,
        "created_at": created_at,
        "user_id": user_id,
        "confidence": confidence,
        "reply": reply,
    }


# ------------------------------------------------------------------
# 构建 StateGraph
# ------------------------------------------------------------------
# 创建状态图实例，状态类型为 RecordState
graph_builder = StateGraph(RecordState)

# 注册节点：record_node 负责持久化记录
graph_builder.add_node("record_node", record_node)

# 定义图的执行流程：
# START（入口）→ record_node（记录节点）→ END（出口）
graph_builder.add_edge(START, "record_node")
graph_builder.add_edge("record_node", END)

# 编译图，生成可执行的 graph 对象
graph = graph_builder.compile()


# ------------------------------------------------------------------
# 主程序：测试用例
# ------------------------------------------------------------------
if __name__ == "__main__":
    """
    本地测试入口。
    提供至少2个用例，验证记录是否正确写入文件，且写入内容可读取、字段完整。
    每次运行测试前会清理测试数据文件，避免历史数据干扰断言。
    """

    # 测试前清理：删除已有数据文件，确保测试环境干净
    # 注意：生产环境不应随意删除文件，此处仅为单元测试需要
    if os.path.exists(EVENTS_FILE):
        os.remove(EVENTS_FILE)

    # 测试用例1：模拟一条物业维修类记录（中等紧急 / 常规场景）
    test_case_1: RecordState = {
        "description": "我家楼下下水道堵了",
        "address": "",
        "event_type": "物业维修",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "物业部",
        "status": "",
        "created_at": "",
        "user_id": "user_001",
        "confidence": "high",
        "reply": "",
    }
    print("=" * 50)
    print("【测试用例1】物业维修 / 中紧急 / 常规")
    print("输入：", test_case_1)
    result_1 = graph.invoke(test_case_1)
    print("输出：", result_1)
    assert result_1["status"] == "已派单", f"status 应为'已派单'，实际为'{result_1['status']}'"
    assert result_1["created_at"] != "", "created_at 不应为空"

    # 测试用例2：模拟一条安全隐患类记录（高度紧急 / 紧急救援场景）
    test_case_2: RecordState = {
        "description": "燃气泄漏了",
        "address": "3号楼1单元",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "紧急救援",
        "handler": "应急救援队（外部资源）",
        "status": "",
        "created_at": "",
        "user_id": "user_002",
        "confidence": "high",
        "reply": "",
    }
    print("=" * 50)
    print("【测试用例2】安全隐患 / 高紧急 / 紧急救援")
    print("输入：", test_case_2)
    result_2 = graph.invoke(test_case_2)
    print("输出：", result_2)
    assert result_2["status"] == "已派单", f"status 应为'已派单'，实际为'{result_2['status']}'"
    assert result_2["created_at"] != "", "created_at 不应为空"

    # 验证文件写入结果：读取 JSONL 文件，确认两条记录均正确持久化
    print("=" * 50)
    print("【文件验证】读取 ./data/events.jsonl")
    assert os.path.exists(EVENTS_FILE), f"文件 {EVENTS_FILE} 未创建"

    with open(EVENTS_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == 2, f"期望文件中有2条记录，实际有{len(lines)}条"

    # 逐行解析并验证字段完整性
    for idx, line in enumerate(lines, start=1):
        record = json.loads(line.strip())
        required_fields = ["description", "address", "event_type", "urgency", "scene_tag", "handler", "status", "created_at", "user_id", "confidence", "reply"]
        for field in required_fields:
            assert field in record, f"第{idx}条记录缺少字段'{field}'"
        assert record["status"] == "已派单", f"第{idx}条记录 status 不正确"
        print(f"第{idx}条记录验证通过：{record['event_type']} | {record['urgency']} | {record['scene_tag']} | {record['handler']}")

    print("=" * 50)
    print("全部测试通过！")
