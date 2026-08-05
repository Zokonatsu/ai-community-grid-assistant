"""
dispatch_agent.py
派发Agent模块（LangGraph单Agent）

功能：根据接收Agent提取的 event_type（事件类型）和 urgency（紧急程度），
      匹配对应的处理部门，并在紧急情况下添加标记，输出包含 handler 的完整 State。

模块接口约定（参见 INTERFACE.md）：
  输入: 接收Agent的完整输出（含 description, address, event_type, urgency, handler）
  输出: {
      "description": "居民原始描述",
      "address": "具体地址",
      "event_type": "物业维修/环境卫生/安全隐患/邻里纠纷/公共设施/其他",
      "urgency": "高/中/低",
      "handler": "物业部/环卫部/安保部/调解员/工程部/综合部"  // 紧急时前缀"[紧急]"
  }
"""

import logging
from typing import TypedDict

from langgraph.graph import StateGraph, START, END

# ------------------------------------------------------------------
# 配置日志记录器
# ------------------------------------------------------------------
logger = logging.getLogger("dispatch_agent")


# ------------------------------------------------------------------
# State 定义
# ------------------------------------------------------------------
class DispatchState(TypedDict):
    """
    派发Agent的状态类型定义。

    字段说明：
        description: 居民提交的原始问题描述（来自接收Agent）。
        address:     从描述中提取的具体地址（来自接收Agent）。
        event_type:  事件分类（来自接收Agent），用于匹配处理部门。
        urgency:     紧急程度（来自接收Agent），"高"时会在 handler 前添加"[紧急]"标记。
        scene_tag:   场景标签（来自接收Agent），"生命急救"或"紧急救援"时会分配外部资源处理方。
        handler:     最终分配的处理方，由本模块根据 event_type、urgency 和 scene_tag 计算得出。
        confidence:  语义校验置信度，透传自接收Agent，本模块不做修改。
    """
    description: str
    address: str
    event_type: str
    urgency: str
    scene_tag: str
    handler: str
    confidence: str


# ------------------------------------------------------------------
# 派单映射表
# ------------------------------------------------------------------
# event_type → handler 的映射关系，便于集中维护和扩展。
# 新增事件类型时，只需在此字典中添加对应项即可。
EVENT_TYPE_TO_HANDLER: dict[str, str] = {
    "物业维修": "物业部",
    "环境卫生": "环卫部",
    "安全隐患": "安保部",
    "邻里纠纷": "调解员",
    "公共设施": "工程部",
    "其他": "综合部",
}


# ------------------------------------------------------------------
# 节点函数：dispatch_node
# ------------------------------------------------------------------
def dispatch_node(state: DispatchState) -> DispatchState:
    """
    LangGraph 节点：根据 event_type、urgency 和 scene_tag 计算并填充 handler 字段。

    执行流程：
        1. 从 state 中读取 event_type、urgency 和 scene_tag。
        2. 若 scene_tag 为"生命急救"，分配"急救中心（外部资源）"。
        3. 若 scene_tag 为"紧急救援"，分配"应急救援队（外部资源）"。
        4. 若 scene_tag 为"常规"，根据 event_type 在映射表中查找对应的处理部门；
           若 event_type 不在映射表中（防御性编程），回退到"综合部"。
        5. 若 urgency 为"高"且 scene_tag 为"常规"，在 handler 前追加"[紧急]"前缀。
        6. 返回包含 handler 的完整 State，其他字段原样保留。

    参数:
        state: 当前图状态，至少包含 event_type、urgency 和 scene_tag 字段。

    返回:
        更新后的 DispatchState，其中 handler 字段已被计算并填充。
    """
    # 读取上游节点传递的关键字段
    event_type = state.get("event_type", "")
    urgency = state.get("urgency", "")
    scene_tag = state.get("scene_tag", "常规")

    # 场景标签优先：生命急救和紧急救援直接分配外部资源处理方
    if scene_tag == "生命急救":
        handler = "急救中心（外部资源）"
    elif scene_tag == "紧急救援":
        handler = "应急救援队（外部资源）"
    else:
        # 常规场景：根据事件类型匹配处理部门，未命中时回退到"综合部"
        handler = EVENT_TYPE_TO_HANDLER.get(event_type, "综合部")
        if event_type not in EVENT_TYPE_TO_HANDLER:
            logger.warning(
                "未知的事件类型 '%s'，回退到综合部处理。description='%s'",
                event_type,
                state.get("description", ""),
            )

        # 紧急标记：若紧急程度为"高"，在 handler 前添加"[紧急]"前缀
        # 仅对常规场景添加，外部资源场景本身已具备最高优先级
        if urgency == "高":
            handler = f"[紧急]{handler}"

    # 构建并返回新的状态对象
    # 保留上游所有字段，仅更新 handler
    return {
        "description": state.get("description", ""),
        "address": state.get("address", ""),
        "event_type": event_type,
        "urgency": urgency,
        "scene_tag": scene_tag,
        "handler": handler,
        "confidence": state.get("confidence", ""),
    }


# ------------------------------------------------------------------
# 构建 StateGraph
# ------------------------------------------------------------------
# 创建状态图实例，状态类型为 DispatchState
graph_builder = StateGraph(DispatchState)

# 注册节点：dispatch_node 负责处理方分配
graph_builder.add_node("dispatch_node", dispatch_node)

# 定义图的执行流程：
# START（入口）→ dispatch_node（派发节点）→ END（出口）
graph_builder.add_edge(START, "dispatch_node")
graph_builder.add_edge("dispatch_node", END)

# 编译图，生成可执行的 graph 对象
graph = graph_builder.compile()


# ------------------------------------------------------------------
# 主程序：测试用例
# ------------------------------------------------------------------
if __name__ == "__main__":
    """
    本地测试入口。
    提供至少3个典型用例，覆盖不同事件类型和紧急程度的组合，验证派单逻辑正确性。
    """

    # 测试用例1：物业维修 + 高紧急 + 常规场景 → 应分配"[紧急]物业部"
    test_case_1: DispatchState = {
        "description": "3号楼电梯故障困人",
        "address": "3号楼",
        "event_type": "物业维修",
        "urgency": "高",
        "scene_tag": "常规",
        "handler": "",
        "confidence": "high",
    }
    print("=" * 50)
    print("【测试用例1】物业维修 / 高紧急 / 常规")
    print("输入：", test_case_1)
    result_1 = graph.invoke(test_case_1)
    print("输出：", result_1)
    assert result_1["handler"] == "[紧急]物业部", f"期望'[紧急]物业部'，实际'{result_1['handler']}'"

    # 测试用例2：邻里纠纷 + 中紧急 + 常规场景 → 应分配"调解员"
    test_case_2: DispatchState = {
        "description": "楼上邻居半夜噪音扰民",
        "address": "5号楼2单元",
        "event_type": "邻里纠纷",
        "urgency": "中",
        "scene_tag": "常规",
        "handler": "",
        "confidence": "high",
    }
    print("=" * 50)
    print("【测试用例2】邻里纠纷 / 中紧急 / 常规")
    print("输入：", test_case_2)
    result_2 = graph.invoke(test_case_2)
    print("输出：", result_2)
    assert result_2["handler"] == "调解员", f"期望'调解员'，实际'{result_2['handler']}'"

    # 测试用例3：其他 + 低紧急 + 常规场景 → 应分配"综合部"
    test_case_3: DispatchState = {
        "description": "建议增加社区图书角",
        "address": "",
        "event_type": "其他",
        "urgency": "低",
        "scene_tag": "常规",
        "handler": "",
        "confidence": "medium",
    }
    print("=" * 50)
    print("【测试用例3】其他 / 低紧急 / 常规")
    print("输入：", test_case_3)
    result_3 = graph.invoke(test_case_3)
    print("输出：", result_3)
    assert result_3["handler"] == "综合部", f"期望'综合部'，实际'{result_3['handler']}'"

    # 测试用例4：生命急救场景 → 应分配"急救中心（外部资源）"
    test_case_4: DispatchState = {
        "description": "有人心脏骤停需要急救",
        "address": "2号楼1单元",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "生命急救",
        "handler": "",
        "confidence": "high",
    }
    print("=" * 50)
    print("【测试用例4】安全隐患 / 高紧急 / 生命急救")
    print("输入：", test_case_4)
    result_4 = graph.invoke(test_case_4)
    print("输出：", result_4)
    assert result_4["handler"] == "急救中心（外部资源）", f"期望'急救中心（外部资源）'，实际'{result_4['handler']}'"

    # 测试用例5：紧急救援场景 → 应分配"应急救援队（外部资源）"
    test_case_5: DispatchState = {
        "description": "小区东门发生火灾",
        "address": "小区东门",
        "event_type": "安全隐患",
        "urgency": "高",
        "scene_tag": "紧急救援",
        "handler": "",
        "confidence": "high",
    }
    print("=" * 50)
    print("【测试用例5】安全隐患 / 高紧急 / 紧急救援")
    print("输入：", test_case_5)
    result_5 = graph.invoke(test_case_5)
    print("输出：", result_5)
    assert result_5["handler"] == "应急救援队（外部资源）", f"期望'应急救援队（外部资源）'，实际'{result_5['handler']}'"

    print("=" * 50)
    print("全部测试通过！")
