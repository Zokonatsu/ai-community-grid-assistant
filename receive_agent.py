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
from concurrent.futures import ThreadPoolExecutor
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


# 模糊急救关键词：短词无上下文时需要前端二次确认
_FUZZY_MEDICAL_RE = re.compile(
    r"吐血|上吊|晕倒|猝死|窒息|中毒",
    re.IGNORECASE,
)
_FUZZY_POLICE_RE = re.compile(
    r"绑架|抢劫|杀人|持刀|行凶",
    re.IGNORECASE,
)
_FUZZY_FIRE_RE = re.compile(
    r"着火|火灾|燃气泄漏|被困|爆炸",
    re.IGNORECASE,
)


def _check_fuzzy_emergency(description: str) -> dict | None:
    """
    模糊急救检查：命中高风险词表且长度≤4字符时，返回需要确认的结果。

    不调用 LLM，不创建任务，仅返回确认标识供前端二次确认。
    """
    cleaned = description.strip()
    if len(cleaned) > 4:
        return None

    if _FUZZY_MEDICAL_RE.search(cleaned):
        return {"confirmation_required": True, "emergency_type": "medical"}
    if _FUZZY_POLICE_RE.search(cleaned):
        return {"confirmation_required": True, "emergency_type": "police"}
    if _FUZZY_FIRE_RE.search(cleaned):
        return {"confirmation_required": True, "emergency_type": "fire"}
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


def _resolve_emergency_type(description: str, scene_tag: str) -> str:
    """
    根据描述和场景标签推断 emergency_type，用于接收模块向派发模块传递明确结论。

    当接收模块已判定为生命急救或紧急救援，但 emergency_type 未设置时，
    通过关键词匹配补全，避免下游派发模块因信息不足而错误 fallback。
    """
    if scene_tag == "生命急救":
        return "medical"
    if scene_tag == "紧急救援":
        # fire 关键词：与 dispatch_agent 的推断规则保持一致
        if _FUZZY_FIRE_RE.search(description) or re.search(
            r"煤气味|燃气味|煤气|燃气", description, re.IGNORECASE
        ):
            return "fire"
        if _FUZZY_POLICE_RE.search(description):
            return "police"
        # 兜底：按描述中更完整的救援类型关键词区分
        if re.search(
            r"火灾|起火|着火|燃气泄漏|煤气泄漏|爆炸|坍塌|电梯困人|高空坠物",
            description,
            re.IGNORECASE,
        ):
            return "fire"
        return "police"
    return ""


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
            "emergency_type": "medical",
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
            "emergency_type": "fire",
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

    投票维度：is_valid、event_type、urgency、scene_tag
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
        return (
            r.get("is_valid"),
            r.get("event_type"),
            r.get("urgency"),
            r.get("scene_tag"),
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
        confirmation_required: 是否需要前端二次确认（模糊急救短词触发）。
        emergency_type: 模糊急救类型，medical/police/fire。
        confirmed:   用户是否已确认高风险描述（用于模糊急救二次提交）。
    """
    description: str
    address: str
    event_type: str
    urgency: str
    scene_tag: str
    handler: str
    confidence: str
    confirmation_required: bool
    emergency_type: str
    confirmed: bool


# ------------------------------------------------------------------
# 系统 Prompt（指导LLM完成信息提取）
# ------------------------------------------------------------------
RECEIVE_SYSTEM_PROMPT = """你是一名社区事务信息提取助手。请根据居民的问题描述，先判断这是否是一条有效的社区事务描述，再提取以下字段并以JSON格式返回。

网格员职责范围定义：
- 正面清单（网格员负责处理）：社区安全、公共设施故障、邻里纠纷、环境卫生、消防安全、物业维修、社区咨询建议等涉及社区公共利益或网格员职责范围内的事件。
- 负面清单（网格员不负责处理）：个人私事（如买菜、做饭、个人出行、逛街）、医疗诊断、商业交易、超出本社区范围的事件、日常闲聊、无意义的问候或测试字符串、家养宠物死亡、个人投资理财、纯个人情感倾诉等。
- 边界模糊判断标准：如果事件不涉及社区公共利益或网格员职责，应判定为管辖外（is_valid 为 false）。

判断规则：
- 如果描述包含具体的社区事务内容（如设施损坏、环境问题、安全隐患、邻里矛盾、咨询建议等），则 is_valid 为 true。
- 如果描述仅为无意义的问候语、闲聊、测试字符串、与社区事务完全无关的内容，则 is_valid 为 false。
- 特别重要：任何涉及人身安全、生命安全、医疗急救、死亡、严重受伤、火灾、燃气泄漏等紧急情况的描述，无论多么简短，都必须视为有效输入（is_valid 为 true）。社区网格员对这类事件负有介入和上报责任，绝不可因描述简短而拒绝。

Few-shot 示例：
- "楼上漏水" → is_valid=true，event_type="物业维修"，urgency="中"，scene_tag="常规"（公共设施/物业维修，涉及邻里共同利益）
- "我去买菜了" → is_valid=false，reject_reason="个人私事，不在网格员管辖范围"（属于个人日常生活，与社区公共利益无关）
- "割腕" → is_valid=true，event_type="安全隐患"，urgency="高"，scene_tag="生命急救"（生命安全紧急情况，网格员必须介入上报）
- "我家狗死了" → is_valid=false，reject_reason="家养宠物死亡，不属于网格员职责范围"（家养宠物事务属于个人私事，不涉及社区公共利益）
- "夫妻吵架声音很大" → is_valid=true，event_type="邻里纠纷"，urgency="中"，scene_tag="常规"（邻里纠纷类，由调解员处理）
- "楼下路灯不亮了" → is_valid=true，event_type="公共设施"，urgency="中"，scene_tag="常规"（公共设施损坏，由工程部处理）
- "楼道里有股煤气味" → is_valid=true，event_type="安全隐患"，urgency="高"，scene_tag="紧急救援"（燃气泄漏安全隐患，由安保部处理并上报119）
- "我想聊聊我的感情问题" → is_valid=false，reject_reason="个人情感倾诉，不属于网格员职责范围"（纯个人情感问题与社区公共事务无关）

字段要求：
1. is_valid（布尔值）：描述是否为有效的社区事务
2. reject_reason（字符串）：当 is_valid 为 false 时，说明拒绝原因；为 true 时返回空字符串 ""
3. address（字符串）：描述中涉及的具体地址或位置信息。如果描述中没有提到具体地址，返回空字符串""
4. event_type（字符串）：事件类型，只能从以下类别中选择一项。选择时必须结合各部门职责范围进行判断：
   - 物业维修（对应处理方：物业部）：房屋维修、水电故障、门禁电梯故障、物业相关投诉、下水道堵塞、管道漏水等
   - 环境卫生（对应处理方：环卫部）：垃圾清理、垃圾分类、绿化养护、异味污染、公共区域清洁等
   - 安全隐患（对应处理方：安保部）：社区治安、安全隐患排查、防盗防骗、可疑人员报告、火灾风险、燃气泄漏、盗窃、高空坠物等
   - 邻里纠纷（对应处理方：调解员）：邻里纠纷、家庭矛盾、噪音扰民、宠物扰民、停车争执、占用公共空间等
   - 公共设施（对应处理方：工程部）：公共设施损坏（路灯、道路、井盖）、基础设施维护、健身器材故障、小区大门损坏等
   - 其他（对应处理方：综合部）：仅当事件确实属于网格员职责范围，且经过仔细判断确实无法归入安保部、物业部、工程部、环卫部、调解员中的任何一类时，才可使用。严禁将个人私事、闲聊、明显无效输入归为"其他"。
5. urgency（字符串）：紧急程度，只能从"高"/"中"/"低"中选择一项：
   - 高：涉及人身安全、火灾、燃气泄漏、电梯困人等紧急情况
   - 中：影响居民正常生活但无直接人身危险，如停水停电、下水道堵塞等
   - 低：一般性建议、咨询、不紧急的改善需求
6. scene_tag（字符串）：场景标签，只能从"生命急救"/"紧急救援"/"常规"中选择一项：
   - 生命急救：涉及人员生命危险，需要医疗急救力量（120）介入，如心脏骤停、严重外伤大出血、突发重病昏迷、窒息、触电致伤、溺水等
   - 紧急救援：需要消防（119）或公安（110）等专业救援力量介入的情况，必须根据描述内容判断具体属于哪类：
     * 119消防职责范围：火灾、起火、着火、燃气泄漏、煤气泄漏、爆炸、建筑物坍塌、电梯困人、高空坠物等
     * 110公安职责范围：暴恐袭击、抢劫、盗窃、打架斗殴、寻仇、吸毒、持刀行凶、绑架、强奸、诈骗、聚众闹事、寻衅滋事、严重交通事故等治安或刑事案件
     * 注意：涉及人身安全威胁、治安犯罪、社会暴力的紧急事件属于110公安，而非119消防
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
        - 纯问候语、纯闲聊、纯测试字符串

    参数:
        description: 居民原始描述。

    返回:
        True 表示通过机械校验，False 表示被拦截。
    """
    if not description or not description.strip():
        return False

    cleaned = description.strip()

    # 生命急救和紧急救援关键词优先放行，不受长度限制
    if _LIFE_RESCUE_RE.search(cleaned) or _EMERGENCY_RESCUE_RE.search(cleaned):
        return True

    # 模糊急救高风险词同样放行，不受长度限制
    if (
        _FUZZY_MEDICAL_RE.search(cleaned)
        or _FUZZY_POLICE_RE.search(cleaned)
        or _FUZZY_FIRE_RE.search(cleaned)
    ):
        return True

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
        if not state.get("confirmed", False):
            hard_result["confirmation_required"] = True
            # emergency_type 已由 _check_hard_rules_first 设置
        else:
            hard_result["confirmation_required"] = False
            hard_result["emergency_type"] = ""
        return hard_result

    # ------------------------------------------------------------------
    # 步骤0.5：模糊急救检查（高风险短词，需要前端二次确认）
    # ------------------------------------------------------------------
    if not state.get("confirmed", False):
        fuzzy_result = _check_fuzzy_emergency(description)
        if fuzzy_result is not None:
            logger.warning(
                "模糊急救命中（%s），返回确认提示：description='%s'",
                fuzzy_result["emergency_type"],
                description,
            )
            return {
                "description": description,
                "address": "",
                "event_type": "安全隐患",
                "urgency": "高",
                "scene_tag": "",
                "handler": "",
                "confidence": "high",
                "confirmation_required": True,
                "emergency_type": fuzzy_result["emergency_type"],
                "confirmed": state.get("confirmed", False),
            }

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
            "confirmation_required": False,
            "emergency_type": "",
        }

    # ------------------------------------------------------------------
    # 步骤2：多轮采样语义校验（消除单次调用随机性）
    # ------------------------------------------------------------------
    # 对同一描述调用多次 LLM API，通过投票统计获得稳定结果。
    # 使用线程池并行执行，将顺序3轮改为并行，总耗时从 3×15s 降至约 1×15s。
    # 任何一轮调用异常均单独捕获，不影响其他轮次。
    parsed_results: list[dict] = []

    def _call_with_hard_rules(desc: str) -> dict:
        parsed = _call_llm_once(desc)
        return _apply_hard_rules(desc, parsed)

    with ThreadPoolExecutor(max_workers=SEMANTIC_CHECK_ROUNDS) as executor:
        futures = [
            executor.submit(_call_with_hard_rules, description)
            for _ in range(SEMANTIC_CHECK_ROUNDS)
        ]
        for future in futures:
            try:
                parsed_results.append(future.result())
            except Exception as exc:
                logger.warning(
                    "语义校验某轮 API 异常，继续收集其他轮次结果。描述='%s'，异常=%s",
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
            "confirmation_required": False,
            "emergency_type": state.get("emergency_type", ""),
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
            scene_tag_val = "生命急救" if _LIFE_RESCUE_RE.search(description) else "紧急救援"
            return {
                "description": description,
                "address": "",
                "event_type": "待审核",
                "urgency": "高",
                "scene_tag": scene_tag_val,
                "handler": "",
                "confidence": "medium",
                "confirmation_required": False,
                "emergency_type": "人工部",
            }

        # 安全兜底：短词（长度≤4字符）即使模型判断无效，也不直接拒绝，降级为待审核
        # 短词缺乏上下文，模型容易误判，优先进入人工审核而非直接拦截
        if len(description.strip()) <= 4:
            logger.warning(
                "语义校验安全兜底：模型判定无效，但输入为短词（长度≤4），降级为待审核。description='%s'",
                description,
            )
            return {
                "description": description,
                "address": "",
                "event_type": "待审核",
                "urgency": "高",
                "scene_tag": "常规",
                "handler": "",
                "confidence": "medium",
                "confirmation_required": False,
                "emergency_type": "人工部",
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
            "confirmation_required": False,
            "emergency_type": state.get("emergency_type", ""),
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
            "confirmation_required": False,
            "emergency_type": "人工部",
        }

    # 若 scene_tag 为外部资源场景但未设置 emergency_type，根据描述推断并传递
    emergency_type_val = state.get("emergency_type", "")
    if not emergency_type_val and scene_tag in ("生命急救", "紧急救援"):
        emergency_type_val = _resolve_emergency_type(description, scene_tag)

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
        "confirmation_required": False,
        "emergency_type": emergency_type_val,
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

    # 测试用例4：家养宠物死亡——应被识别为无效输入
    test_case_4 = "我家狗死了"
    print("=" * 50)
    print("【测试用例4】输入：", test_case_4)
    result_4 = graph.invoke({"description": test_case_4})
    print("输出结果：")
    print(f"  address    : {result_4['address']}")
    print(f"  event_type : {result_4['event_type']}")
    print(f"  urgency    : {result_4['urgency']}")
    print(f"  scene_tag  : {result_4['scene_tag']}")
    print(f"  handler    : {result_4['handler']}")
    print(f"  confidence : {result_4.get('confidence', 'N/A')}")

    # 测试用例5：邻里纠纷——应由调解员处理
    test_case_5 = "夫妻吵架声音很大"
    print("=" * 50)
    print("【测试用例5】输入：", test_case_5)
    result_5 = graph.invoke({"description": test_case_5})
    print("输出结果：")
    print(f"  address    : {result_5['address']}")
    print(f"  event_type : {result_5['event_type']}")
    print(f"  urgency    : {result_5['urgency']}")
    print(f"  scene_tag  : {result_5['scene_tag']}")
    print(f"  handler    : {result_5['handler']}")
    print(f"  confidence : {result_5.get('confidence', 'N/A')}")

    # 测试用例6：公共设施损坏——应由工程部处理
    test_case_6 = "楼下路灯不亮了"
    print("=" * 50)
    print("【测试用例6】输入：", test_case_6)
    result_6 = graph.invoke({"description": test_case_6})
    print("输出结果：")
    print(f"  address    : {result_6['address']}")
    print(f"  event_type : {result_6['event_type']}")
    print(f"  urgency    : {result_6['urgency']}")
    print(f"  scene_tag  : {result_6['scene_tag']}")
    print(f"  handler    : {result_6['handler']}")
    print(f"  confidence : {result_6.get('confidence', 'N/A')}")

    # 测试用例7：个人情感倾诉——应被识别为无效输入
    test_case_7 = "我想聊聊我的感情问题"
    print("=" * 50)
    print("【测试用例7】输入：", test_case_7)
    result_7 = graph.invoke({"description": test_case_7})
    print("输出结果：")
    print(f"  address    : {result_7['address']}")
    print(f"  event_type : {result_7['event_type']}")
    print(f"  urgency    : {result_7['urgency']}")
    print(f"  scene_tag  : {result_7['scene_tag']}")
    print(f"  handler    : {result_7['handler']}")
    print(f"  confidence : {result_7.get('confidence', 'N/A')}")

    # 测试用例8：安全隐患——应由安保部处理并触发紧急救援
    test_case_8 = "楼道里有股煤气味"
    print("=" * 50)
    print("【测试用例8】输入：", test_case_8)
    result_8 = graph.invoke({"description": test_case_8})
    print("输出结果：")
    print(f"  address    : {result_8['address']}")
    print(f"  event_type : {result_8['event_type']}")
    print(f"  urgency    : {result_8['urgency']}")
    print(f"  scene_tag  : {result_8['scene_tag']}")
    print(f"  handler    : {result_8['handler']}")
    print(f"  confidence : {result_8.get('confidence', 'N/A')}")

    print("=" * 50)
    print("测试完成。")
