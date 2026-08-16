"""
workflow.py
社区事件处理完整工作流

功能：将接收Agent（receive_agent）、派发Agent（dispatch_agent）和记录Agent（record_agent）
      串联为完整链路，实现从居民原始描述到最终派单结果、再到本地持久化的端到端自动化处理。

处理链路：
  START → receive_node（信息提取：地址/事件类型/紧急程度）
        → dispatch_node（派单分配：匹配处理部门 + 紧急标记）
        → record_node（持久化记录：补充状态和时间戳，写入JSONL文件）
        → END

输入：居民原始描述字符串（如"我家楼下下水道堵了"）
输出：完整派单结果（含 address, event_type, urgency, handler, status, created_at 等字段）
"""

import json
import os
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

# ------------------------------------------------------------------
# 复用已有Agent的节点函数和State定义
# ------------------------------------------------------------------
# 约束：不重复编写提取/派单/记录逻辑，直接导入三个Agent模块中的节点函数。
# 导入时会自动执行模块级初始化（如加载.env、初始化API客户端等）。
import receive_agent
import dispatch_agent
import record_agent

# 复用 receive_agent 的节点函数（负责调用LLM API提取结构化信息）
receive_node = receive_agent.receive_node

# 复用 dispatch_agent 的节点函数（负责根据event_type和urgency分配处理方）
dispatch_node = dispatch_agent.dispatch_node

# 复用 record_agent 的节点函数（负责将完整结果持久化到本地JSONL文件）
record_node = record_agent.record_node


# ------------------------------------------------------------------
# State 定义（统一链路状态）
# ------------------------------------------------------------------
# 三个Agent的State字段完全兼容：
#   receive_agent 和 dispatch_agent 共享 description, address, event_type, urgency, handler
#   record_agent 额外需要 status 和 created_at（由 record_node 自身填充）
# 因此 WorkflowState 需包含全部 7 个字段，确保数据在整个链路中无缝传递。
class WorkflowState(TypedDict):
    """
    完整工作流的状态类型定义。

    字段说明：
        description: 居民提交的原始问题描述（初始输入）。
        address:     接收Agent从描述中提取的具体地址。
        event_type:  接收Agent提取的事件分类（用于派发Agent匹配部门）。
        urgency:     接收Agent提取的紧急程度（"高"时派发Agent会添加"[紧急]"前缀）。
        scene_tag:   接收Agent提取的场景标签（"生命急救"/"紧急救援"/"常规"），影响派发行为。
        handler:     派发Agent最终分配的处理部门（工作流输出核心字段）。
        status:      记录Agent填充的状态（"已派单"/"待审核"），初始为空字符串。
        created_at:  记录Agent填充的创建时间（格式"YYYY-MM-DD HH:MM:SS"），初始为空字符串。
        user_id:     提交事件的用户标识，用于数据隔离与审计追溯。
        confidence:  语义校验置信度（high/medium/low/none），由接收Agent填充。
        confirmation_required: 是否需要前端二次确认（模糊急救短词触发）。
        emergency_type: 模糊急救类型（medical/police/fire）。
            注意：当前端二次提交（confirmed=true）时，receive_node 可能将该字段清空为空字符串；
            dispatch_agent 已通过 description 关键词推断做兜底恢复，确保 110/119/120 正确派单。
        confirmed:   用户是否已确认高风险描述（用于模糊急救二次提交）。
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
    confirmation_required: bool
    emergency_type: str
    confirmed: bool


# ------------------------------------------------------------------
# 条件路由：接收节点之后
# ------------------------------------------------------------------
def _route_after_receive(state: WorkflowState) -> str:
    """
    条件路由：根据接收Agent的输出决定后续走向。

    若 event_type 为'无效输入'、'API异常'或'待审核'，直接结束工作流，
    不调用派发Agent和记录Agent，避免生成无意义工单或错误派单。
    """
    if state.get("event_type") in ("无效输入", "API异常", "待审核"):
        return END
    return "dispatch_node"


# ------------------------------------------------------------------
# 构建完整工作流 StateGraph
# ------------------------------------------------------------------
# 创建状态图实例，状态类型为 WorkflowState
workflow_builder = StateGraph(WorkflowState)

# 注册接收节点：调用LLM API提取结构化信息
workflow_builder.add_node("receive_node", receive_node)

# 注册派发节点：根据提取结果匹配处理部门
workflow_builder.add_node("dispatch_node", dispatch_node)

# 注册记录节点：将完整结果持久化到本地JSONL文件
workflow_builder.add_node("record_node", record_node)

# 定义图的执行流程：
# START（入口）→ receive_node（信息提取）→ dispatch_node（派单分配）
#               → record_node（持久化记录）→ END（出口）
# 对无效输入，receive_node 后直接结束，跳过派单和记录。
workflow_builder.add_edge(START, "receive_node")
workflow_builder.add_conditional_edges("receive_node", _route_after_receive)
workflow_builder.add_edge("dispatch_node", "record_node")
workflow_builder.add_edge("record_node", END)

# 编译图，生成可执行的工作流对象
workflow = workflow_builder.compile()


# ------------------------------------------------------------------
# 简化工作流：跳过语义校验，直接派单+记录
# ------------------------------------------------------------------
# 在 main.py 中，同步语义校验已在请求处理时完成。
# 后台任务直接复用校验结果，避免二次调用 LLM API 导致结果不一致或超时。
dispatch_record_builder = StateGraph(WorkflowState)
dispatch_record_builder.add_node("dispatch_node", dispatch_node)
dispatch_record_builder.add_node("record_node", record_node)
dispatch_record_builder.add_edge(START, "dispatch_node")
dispatch_record_builder.add_edge("dispatch_node", "record_node")
dispatch_record_builder.add_edge("record_node", END)
dispatch_record_workflow = dispatch_record_builder.compile()


# ------------------------------------------------------------------
# 主程序：端到端测试
# ------------------------------------------------------------------
if __name__ == "__main__":
    """
    本地测试入口。
    提供至少2个端到端用例，输入为居民原始描述字符串，输出为完整派单结果。
    同时验证 record_agent 是否已将记录正确写入 ./data/events.jsonl 文件。
    覆盖普通紧急和高度紧急场景，验证全链路协作正确性。
    """

    # 测试前清理数据文件，避免历史记录干扰本次验证
    events_file = "./data/events.jsonl"
    if os.path.exists(events_file):
        os.remove(events_file)

    def run_end_to_end(description: str) -> WorkflowState:
        """
        辅助函数：运行端到端工作流并打印结果。

        参数:
            description: 居民原始问题描述。

        返回:
            工作流执行后的完整状态（含 status 和 created_at）。
        """
        # 初始状态仅需提供 description，其他字段由各Agent逐步填充
        initial_state: WorkflowState = {
            "description": description,
            "address": "",
            "event_type": "",
            "urgency": "",
            "scene_tag": "",
            "handler": "",
            "status": "",
            "created_at": "",
            "user_id": "",
            "confidence": "",
            "confirmation_required": False,
            "emergency_type": "",
            "confirmed": False,
        }

        print(f"\n【输入描述】{description}")
        print("-" * 40)

        # 调用完整工作流，state 在三个节点间自动传递：
        # receive_node → dispatch_node → record_node
        result = workflow.invoke(initial_state)

        print("【最终派单结果】")
        print(f"  地址(address)      : {result['address']}")
        print(f"  事件类型(event_type) : {result['event_type']}")
        print(f"  紧急程度(urgency)   : {result['urgency']}")
        print(f"  场景标签(scene_tag) : {result['scene_tag']}")
        print(f"  处理方(handler)     : {result['handler']}")
        print(f"  状态(status)       : {result['status']}")
        print(f"  创建时间(created_at) : {result['created_at']}")
        print("=" * 50)

        return result

    # 测试用例1：物业维修类（中等紧急 / 常规场景）—— 端到端验证常规流程
    # 预期结果：event_type=物业维修, urgency=中, scene_tag=常规, handler=物业部, status=已派单
    result_1 = run_end_to_end("我家楼下下水道堵了")
    assert result_1["status"] == "已派单", f"status 应为'已派单'，实际为'{result_1['status']}'"
    assert result_1["created_at"] != "", "created_at 不应为空"

    # 测试用例2：安全隐患类（高度紧急 / 紧急救援场景）—— 端到端验证外部资源派单
    # 预期结果：event_type=安全隐患, urgency=高, scene_tag=紧急救援, handler=应急救援队（外部资源）, status=已派单
    result_2 = run_end_to_end("燃气泄漏了")
    assert result_2["status"] == "已派单", f"status 应为'已派单'，实际为'{result_2['status']}'"
    assert result_2["created_at"] != "", "created_at 不应为空"

    # 测试用例3：模糊急救 police（二次提交，confirmed=true，emergency_type 被清空）
    # 预期：dispatch_agent 通过 description 关键词推断，派给 110公安急救中心
    initial_police: WorkflowState = {
        "description": "绑架",
        "address": "",
        "event_type": "",
        "urgency": "",
        "scene_tag": "",
        "handler": "",
        "status": "",
        "created_at": "",
        "user_id": "",
        "confidence": "",
        "confirmation_required": False,
        "emergency_type": "",
        "confirmed": True,
    }
    result_3 = workflow.invoke(initial_police)
    assert result_3["handler"] == "110公安急救中心（外部资源）", f"police 应为'110公安急救中心（外部资源）'，实际为'{result_3['handler']}'"
    print("【测试用例3】police 模糊急救二次提交 —— 通过")

    # 测试用例4：模糊急救 fire（二次提交，confirmed=true，emergency_type 被清空）
    # 预期：dispatch_agent 通过 description 关键词推断，派给 119消防急救中心
    initial_fire: WorkflowState = {
        "description": "爆炸",
        "address": "",
        "event_type": "",
        "urgency": "",
        "scene_tag": "",
        "handler": "",
        "status": "",
        "created_at": "",
        "user_id": "",
        "confidence": "",
        "confirmation_required": False,
        "emergency_type": "",
        "confirmed": True,
    }
    result_4 = workflow.invoke(initial_fire)
    assert result_4["handler"] == "119消防急救中心（外部资源）", f"fire 应为'119消防急救中心（外部资源）'，实际为'{result_4['handler']}'"
    print("【测试用例4】fire 模糊急救二次提交 —— 通过")

    # 验证文件写入结果：读取 JSONL 文件，确认记录均已持久化
    print("\n【文件验证】读取 ./data/events.jsonl")
    assert os.path.exists(events_file), f"文件 {events_file} 未创建"

    with open(events_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == 4, f"期望文件中有4条记录，实际有{len(lines)}条"

    for idx, line in enumerate(lines, start=1):
        record = json.loads(line.strip())
        required_fields = ["description", "address", "event_type", "urgency", "scene_tag", "handler", "status", "created_at"]
        for field in required_fields:
            assert field in record, f"第{idx}条记录缺少字段'{field}'"
        assert record["status"] == "已派单", f"第{idx}条记录 status 不正确"
        print(f"第{idx}条记录验证通过：{record['event_type']} | {record['urgency']} | {record['scene_tag']} | {record['handler']}")

    print("\n端到端测试完成，全部通过。")
