"""
receive_agent.py
接收Agent模块（LangGraph单Agent）

功能：接收居民的原始问题描述，通过LLM API提取关键信息（地址、事件类型、紧急程度），
      输出结构化的State数据，为后续派发Agent提供输入。

模块接口约定（参见 INTERFACE.md）：
  输入: {"description": "居民原始描述"}
  输出: {
      "address": "具体地址",
      "event_type": "物业维修/环境卫生/安全隐患/邻里纠纷/公共设施/其他",
      "urgency": "高/中/低",
      "handler": ""  // 初始为空，由派发Agent填充
  }
"""

import json
import logging
import re
from typing import TypedDict

from openai import OpenAI
from langgraph.graph import StateGraph, START, END

import config

# ------------------------------------------------------------------
# 配置日志记录器
# ------------------------------------------------------------------
# 使用模块级 logger，便于生产环境按模块粒度控制日志级别和输出目标。
# 默认级别为 WARNING，可在运行时通过 logging.getLogger("receive_agent").setLevel(...) 调整。
logger = logging.getLogger("receive_agent")

# ------------------------------------------------------------------
# 初始化 OpenAI 客户端（用于调用LLM API，底层兼容OpenAI SDK格式）
# max_retries=0：禁用 SDK 自动重试，避免重试累积耗时触发上层 asyncio.wait_for 超时
client = OpenAI(
    api_key=config.LLM_API_KEY,
    base_url=config.LLM_BASE_URL,
    max_retries=0,
)


# ------------------------------------------------------------------
# 语义校验多轮采样配置
# ------------------------------------------------------------------
# 对同一输入并行调用多次 LLM API，通过投票消除单次调用的随机性波动。
# 单次 timeout 设为 15 秒，3 轮累计不超过 45 秒，与原有超时策略一致。
SEMANTIC_CHECK_ROUNDS: int = 3
_SEMANTIC_SINGLE_TIMEOUT: float = 15.0

# ------------------------------------------------------------------
# 硬规则辅助：确定性关键词匹配
# ------------------------------------------------------------------
# 当模型对边界输入判断不一致时，硬规则提供兜底确定性。
# 规则优先级低于模型但高于投票统计，命中即强制覆盖对应标签。
_LIFE_RESCUE_RE = re.compile(
    r"心脏骤停|心跳停止|心肺复苏|大出血|昏迷|窒息|触电|电击伤|电击|突发重病|心梗|心肌梗死|脑溢血|中风|溺水|"
    r"人死了|有人死|死人|去世|身亡|猝死|割腕|自杀|自残|跳楼|轻生|煤气中毒",
    re.IGNORECASE,
)
_EMERGENCY_RESCUE_RE = re.compile(
    r"火灾|起火|着火|燃气泄漏|煤气泄漏|电梯困人|建筑物坍塌|坍塌|严重交通事故|爆炸|高空坠物",
    re.IGNORECASE,
)


def _apply_hard_rules(description: str, parsed: dict) -> dict:
    """
    对模型返回结果应用硬规则兜底。

    命中生命急救或紧急救援关键词时，强制覆盖对应字段，
    确保涉及人身安全的输入不会因为模型波动而被误判。
    """
    if _LIFE_RESCUE_RE.search(description):
        parsed["is_valid"] = True
        parsed["scene_tag"] = "生命急救"
        parsed["urgency"] = "高"
    elif _EMERGENCY_RESCUE_RE.search(description):
        parsed["is_valid"] = True
        parsed["scene_tag"] = "紧急救援"
        parsed["urgency"] = "高"
    return parsed


def _check_hard_rules_first(description: str) -> dict | None:
    """
    前置硬规则检查：在调用任何 LLM API 之前执行。

    命中生命急救或紧急救援关键词时，直接返回结构化结果，
    跳过所有 LLM 调用和多轮投票，确保生命安全类消息零延迟处理。
    """
    if _LIFE_RESCUE_RE.search(description):
        return {
            "description": description,
            "address": "",
            "event_type": "安全隐患",
            "urgency": "高",
            "scene_tag": "生命急救",
            "handler": "",
            "confidence": "high",
        }
    if _EMERGENCY_RESCUE_RE.search(description):
        return {
            "description": description,
            "address": "",
            "event_type": "安全隐患",
            "urgency": "高",
            "scene_tag": "紧急救援",
            "handler": "",
            "confidence": "high",
        }
    return None


# ------------------------------------------------------------------
# 单次 LLM API 调用（隔离异常，便于多轮采样）
# ------------------------------------------------------------------
def _call_llm_once(description: str) -> dict:
    """
    单次调用 LLM API 进行语义提取。

    返回模型解析后的字典；任何异常均向上抛出，由调用方决定是否重试。
    """
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": RECEIVE_SYSTEM_PROMPT},
            {"role": "user", "content": description},
        ],
        response_format={"type": "json_object"},
        timeout=_SEMANTIC_SINGLE_TIMEOUT,
    )
    content = response.choices[0].message.content
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError(f"模型返回非字典类型: {type(parsed)}")
    return parsed


# ------------------------------------------------------------------
# 多轮结果投票与置信度计算
# ------------------------------------------------------------------
def _vote_on_results(results: list[dict]) -> tuple[dict, str]:
    """
    对多轮采样结果进行投票，返回最可信的结果及置信度。

    投票维度：is_valid、event_type、urgency、scene_tag、address
    置信度规则：
        - high  ：所有结果完全一致
        - medium：存在多数（≥2/3）一致的结果
        - low   ：结果分散，无明确多数，需人工审核

    参数:
        results: 多轮 API 调用的解析结果列表，长度 ≥1。

    返回:
        (merged_result, confidence)  其中 confidence ∈ {"high", "medium", "low"}
    """
    if not results:
        raise ValueError("投票至少需要一条结果")

    if len(results) == 1:
        return results[0], "medium"

    def _result_key(r: dict):
        # address 允许为空，但为空时属于高风险场景，在后续逻辑中单独处理
        return (
            r.get("is_valid"),
            r.get("event_type"),
            r.get("urgency"),
            r.get("scene_tag"),
            r.get("address", ""),
        )

    from collections import Counter

    keys = [_result_key(r) for r in results]
    counter = Counter(keys)
    most_common_key, most_common_count = counter.most_common(1)[0]

    total = len(results)
    if most_common_count == total:
        confidence = "high"
    elif most_common_count >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    # 取多数结果的第一个完整字典作为代表
    for r in results:
        if _result_key(r) == most_common_key:
            return r, confidence

    # 兜底：理论上不会到达此处
    return results[0], "low"


# ------------------------------------------------------------------
# State 定义
# ------------------------------------------------------------------
class ReceiveState(TypedDict):
    """
    接收Agent的状态类型定义。

    字段说明：
        description: 居民提交的原始问题描述（必填输入）。
        address:     从描述中提取的具体地址，如"3号楼2单元"、"小区东门"等。
        event_type:  事件分类，限定为：物业维修/环境卫生/安全隐患/邻里纠纷/公共设施/其他/无效输入/API异常/待审核。
        urgency:     紧急程度，限定为：高/中/低。
        scene_tag:   场景标签，限定为：生命急救/紧急救援/常规。根据描述自动判断。
        handler:     处理方标识，初始为空字符串 ""，由派发Agent后续填充。
        confidence:  语义校验置信度，high/medium/low/none，由多轮投票计算得出。
    """
    description: str
    address: str
    event_type: str
    urgency: str
    scene_tag: str
    handler: str
    confidence: str


# ------------------------------------------------------------------
# 系统 Prompt（指导LLM完成信息提取）
# ------------------------------------------------------------------
RECEIVE_SYSTEM_PROMPT = """你是一名社区事务信息提取助手。请根据居民的问题描述，先判断这是否是一条有效的社区事务描述，再提取以下字段并以JSON格式返回。

判断规则：
- 如果描述包含具体的社区事务内容（如设施损坏、环境问题、安全隐患、邻里矛盾、咨询建议等），则 is_valid 为 true。
- 如果描述仅为无意义的问候语、闲聊、测试字符串、与社区事务完全无关的内容，则 is_valid 为 false。
- 特别重要：任何涉及人身安全、生命安全、医疗急救、死亡、严重受伤、火灾、燃气泄漏等紧急情况的描述，无论多么简短，都必须视为有效输入（is_valid 为 true）。社区网格员对这类事件负有介入和上报责任，绝不可因描述简短而拒绝。

字段要求：
1. is_valid（布尔值）：描述是否为有效的社区事务
2. reject_reason（字符串）：当 is_valid 为 false 时，说明拒绝原因；为 true 时返回空字符串 ""
3. address（字符串）：描述中涉及的具体地址或位置信息。如果描述中没有提到具体地址，返回空字符串""
4. event_type（字符串）：事件类型，只能从以下类别中选择一项：
   - 物业维修：如水电故障、房屋维修、电梯问题、下水道堵塞等
   - 环境卫生：如垃圾清理、绿化养护、异味污染等
   - 安全隐患：如火灾风险、燃气泄漏、盗窃、高空坠物等
   - 邻里纠纷：如噪音扰民、宠物管理、停车争执、占用公共空间等
   - 公共设施：如路灯损坏、道路破损、健身器材故障、门禁失灵等
   - 其他：不属于以上类别的特殊情况
5. urgency（字符串）：紧急程度，只能从"高"/"中"/"低"中选择一项：
   - 高：涉及人身安全、火灾、燃气泄漏、电梯困人等紧急情况
   - 中：影响居民正常生活但无直接人身危险，如停水停电、下水道堵塞等
   - 低：一般性建议、咨询、不紧急的改善需求
6. scene_tag（字符串）：场景标签，只能从"生命急救"/"紧急救援"/"常规"中选择一项：
   - 生命急救：涉及人员生命危险，需要医疗急救力量介入，如心脏骤停、严重外伤大出血、突发重病昏迷、窒息、触电致伤等
   - 紧急救援：涉及火灾、燃气泄漏、电梯困人、溺水、建筑物坍塌、严重交通事故等需要消防/公安/专业救援力量介入的情况
   - 常规：一般社区事务，不需要外部专业急救或救援力量介入

输出格式要求：
- 必须返回合法的JSON对象，不要包含任何Markdown代码块标记或额外解释文字
- JSON格式示例：{"is_valid":true,"reject_reason":"","address":"5号楼1单元","event_type":"物业维修","urgency":"中","scene_tag":"常规"}
- 无效输入示例：{"is_valid":false,"reject_reason":"纯问候语，无实质事务内容","address":"","event_type":"","urgency":"","scene_tag":""}
"""


# ------------------------------------------------------------------
# 输入有效性校验（辅助层）
# ------------------------------------------------------------------
_INVALID_INPUT_RE = re.compile(
    r"^(?:"
    r"你好|您好|哈喽|嗨|hi|hello|hey|在吗|在么|有人吗|早上好|下午好|晚上好|晚安|再见|拜拜|谢谢|感谢|你们好|大家好|吃了吗|我们好|"
    r"test|测试|hello\s*world|123+|哈哈哈+|嘻嘻+|呵呵+|呜呜+|嗯+|啊+|哦+|哇+|哎+"
    r")[！!。.,~？?呀哈呐呢吧]*$",
    re.IGNORECASE,
)

def _is_valid_input(description: str) -> bool:
    """
    前置机械校验：过滤绝对无意义的输入。

    该函数仅作为辅助校验，不做语义判断。
    语义有效性由 LLM API 通过 is_valid 字段判定，优先于本函数。

    无效输入特征：
        - 空字符串或仅包含空白字符
        - 纯数字、纯标点符号等无意义内容
        - 总长度不足3个字符

    参数:
        description: 居民原始描述。

    返回:
        True 表示通过机械校验，False 表示被拦截。
    """
    if not description or not description.strip():
        return False

    cleaned = description.strip()

    # 长度不足3个字符，直接视为无效
    if len(cleaned) < 3:
        return False

    # 纯数字或纯标点符号（无任何字母/汉字）
    if cleaned.isdigit() or all(not c.isalnum() for c in cleaned):
        return False

    # 纯问候语、纯闲聊、纯测试字符串等明显无效输入
    if _INVALID_INPUT_RE.match(cleaned):
        return False

    return True


def _validate_address(address: str, description: str) -> None:
    """
    对提取出的 address 进行基本校验，发现问题时记录警告日志。

    校验规则：
        - 为空：记录警告，提示未提取到地址
        - 长度小于2或纯数字：记录警告，提示地址可能不合理
    """
    if not address:
        logger.warning(
            "未提取到地址信息，description='%s'",
            description,
        )
    elif len(address) < 2 or address.isdigit():
        logger.warning(
            "提取的地址信息可能不合理，address='%s'，description='%s'",
            address,
            description,
        )


# ------------------------------------------------------------------
# 节点函数：receive_node
# ------------------------------------------------------------------
def receive_node(state: ReceiveState) -> ReceiveState:
    """
    LangGraph 节点：接收居民描述，调用 LLM API 提取结构化信息。

    执行流程：
        1. 从 state 中读取 description（居民原始描述）。
        2. **前置机械校验**：对绝对无意义输入（空、纯数字、纯标点、长度<3）
           直接拒绝，不调用 API。本层仅作辅助，不做语义判断。
        3. 调用 LLM API（openai库），传入系统Prompt + 用户描述。
           模型通过语义理解判断 is_valid，优先于固定规则。
        4. 解析模型返回的JSON，检查 is_valid 字段。
           若 is_valid 为 false，返回 event_type="无效输入"，不生成工单。
        5. 提取 address、event_type、urgency。
        6. 对提取出的 address 进行基本校验，发现问题时记录警告日志。
        7. 若 API 调用或 JSON 解析异常，返回 event_type="API异常"，
           明确标记错误，不生成无意义工单。
        8. 保持 handler 字段为空字符串（派发Agent负责填充）。

    参数:
        state: 当前图状态，至少包含 description 字段。

    返回:
        更新后的 ReceiveState。对无效输入，event_type 固定为"无效输入"；
        对 API 异常，event_type 固定为"API异常"；
        对有效输入，包含提取出的 address、event_type、urgency、scene_tag 及初始空的 handler。
    """
    # 读取输入描述
    description = state.get("description", "")

    # ------------------------------------------------------------------
    # 步骤0：前置硬规则检查（生命安全优先，跳过所有LLM调用）
    # ------------------------------------------------------------------
    hard_result = _check_hard_rules_first(description)
    if hard_result is not None:
        logger.warning(
            "前置硬规则命中（%s），跳过LLM调用：description='%s'",
            hard_result["scene_tag"],
            description,
        )
        return hard_result

    # ------------------------------------------------------------------
    # 步骤1：前置机械校验（辅助层）
    # ------------------------------------------------------------------
    if not _is_valid_input(description):
        logger.warning(
            "前置机械校验拦截：description='%s'",
            description,
        )
        return {
            "description": description,
            "address": "",
            "event_type": "无效输入",
            "urgency": "低",
            "scene_tag": "常规",
            "handler": "",
            "confidence": "high",
        }

    # ------------------------------------------------------------------
    # 步骤2：多轮采样语义校验（消除单次调用随机性）
    # ------------------------------------------------------------------
    # 对同一描述调用多次 LLM API，通过投票统计获得稳定结果。
    # 任何一轮调用异常均单独捕获，不影响其他轮次。
    parsed_results: list[dict] = []
    for round_idx in range(SEMANTIC_CHECK_ROUNDS):
        try:
            parsed = _call_llm_once(description)
            parsed = _apply_hard_rules(description, parsed)
            parsed_results.append(parsed)
        except Exception as exc:
            logger.warning(
                "语义校验第 %d 轮 API 异常，继续尝试下一轮。描述='%s'，异常=%s",
                round_idx + 1,
                description,
                exc,
            )
            continue

    # 所有轮次均失败 -> 明确标记为 API异常
    if not parsed_results:
        logger.error(
            "语义校验全部 %d 轮均失败，标记为 API异常。描述='%s'",
            SEMANTIC_CHECK_ROUNDS,
            description,
        )
        return {
            "description": description,
            "address": "",
            "event_type": "API异常",
            "urgency": "低",
            "scene_tag": "常规",
            "handler": "",
            "confidence": "none",
        }

    # 投票：取多数结果并计算置信度
    merged, confidence = _vote_on_results(parsed_results)

    # ------------------------------------------------------------------
    # 步骤3：语义有效性判断
    # ------------------------------------------------------------------
    is_valid = merged.get("is_valid", True)
    if not is_valid:
        # 安全兜底：涉及生命安全/紧急救援的输入，即使模型判断无效，也不直接拒绝
        if _LIFE_RESCUE_RE.search(description) or _EMERGENCY_RESCUE_RE.search(description):
            logger.warning(
                "语义校验安全兜底：模型判定无效，但命中紧急关键词，降级为待审核。description='%s'",
                description,
            )
            return {
                "description": description,
                "address": "",
                "event_type": "待审核",
                "urgency": "高",
                "scene_tag": "生命急救" if _LIFE_RESCUE_RE.search(description) else "紧急救援",
                "handler": "",
                "confidence": "medium",
            }

        reject_reason = merged.get("reject_reason", "语义判断为无效输入")
        logger.warning(
            "语义校验拦截（置信度=%s）：%s。description='%s'",
            confidence,
            reject_reason,
            description,
        )
        return {
            "description": description,
            "address": "",
            "event_type": "无效输入",
            "urgency": "低",
            "scene_tag": "常规",
            "handler": "",
            "confidence": confidence,
        }

    # 提取字段，若缺失则使用安全默认值
    address = merged.get("address", "")
    event_type = merged.get("event_type", "其他")
    urgency = merged.get("urgency", "中")
    scene_tag = merged.get("scene_tag", "常规")
    # 防御性校验：若模型返回非预期值，回退到常规
    if scene_tag not in ("生命急救", "紧急救援", "常规"):
        scene_tag = "常规"

    # ------------------------------------------------------------------
    # 步骤4：address 基本校验 + 缺失拦截
    # ------------------------------------------------------------------
    _validate_address(address, description)

    # 置信度低 -> 进入待审核状态，不直接派单
    if confidence == "low":
        logger.warning(
            "输入进入待审核状态（语义置信度低）：description='%s'，address='%s'，confidence=%s",
            description,
            address,
            confidence,
        )
        return {
            "description": description,
            "address": address,
            "event_type": "待审核",
            "urgency": urgency,
            "scene_tag": scene_tag,
            "handler": "",
            "confidence": confidence,
        }

    # 构建并返回新的状态对象
    # handler 始终初始化为空字符串，不由接收Agent决定处理方
    return {
        "description": description,
        "address": address,
        "event_type": event_type,
        "urgency": urgency,
        "scene_tag": scene_tag,
        "handler": "",
        "confidence": confidence,
    }


# ------------------------------------------------------------------
# 构建 StateGraph
# ------------------------------------------------------------------
# 创建状态图实例，状态类型为 ReceiveState
graph_builder = StateGraph(ReceiveState)

# 注册节点：receive_node 负责信息提取
graph_builder.add_node("receive_node", receive_node)

# 定义图的执行流程：
# START（入口）→ receive_node（信息提取节点）→ END（出口）
graph_builder.add_edge(START, "receive_node")
graph_builder.add_edge("receive_node", END)

# 编译图，生成可执行的 graph 对象
graph = graph_builder.compile()


# ------------------------------------------------------------------
# 主程序：测试用例
# ------------------------------------------------------------------
if __name__ == "__main__":
    """
    本地测试入口。
    提供至少2个典型用例，覆盖不同的事件类型和紧急程度，验证Agent提取能力。
    """

    # 测试用例1：物业维修类（中等紧急）—— 下水道堵塞影响生活
    test_case_1 = "我家楼下下水道堵了"
    print("=" * 50)
    print("【测试用例1】输入：", test_case_1)
    result_1 = graph.invoke({"description": test_case_1})
    print("输出结果：")
    print(f"  address    : {result_1['address']}")
    print(f"  event_type : {result_1['event_type']}")
    print(f"  urgency    : {result_1['urgency']}")
    print(f"  scene_tag  : {result_1['scene_tag']}")
    print(f"  handler    : {result_1['handler']}")
    print(f"  confidence : {result_1.get('confidence', 'N/A')}")

    # 测试用例2：公共设施类（中等紧急）—— 路灯损坏影响出行安全
    test_case_2 = "小区东门路灯坏了"
    print("=" * 50)
    print("【测试用例2】输入：", test_case_2)
    result_2 = graph.invoke({"description": test_case_2})
    print("输出结果：")
    print(f"  address    : {result_2['address']}")
    print(f"  event_type : {result_2['event_type']}")
    print(f"  urgency    : {result_2['urgency']}")
    print(f"  scene_tag  : {result_2['scene_tag']}")
    print(f"  handler    : {result_2['handler']}")
    print(f"  confidence : {result_2.get('confidence', 'N/A')}")

    # 测试用例3：生命急救场景
    test_case_3 = "3号楼有人心脏骤停，需要急救"
    print("=" * 50)
    print("【测试用例3】输入：", test_case_3)
    result_3 = graph.invoke({"description": test_case_3})
    print("输出结果：")
    print(f"  address    : {result_3['address']}")
    print(f"  event_type : {result_3['event_type']}")
    print(f"  urgency    : {result_3['urgency']}")
    print(f"  scene_tag  : {result_3['scene_tag']}")
    print(f"  handler    : {result_3['handler']}")
    print(f"  confidence : {result_3.get('confidence', 'N/A')}")

    print("=" * 50)
    print("测试完成。")
